from __future__ import annotations

import re
from dataclasses import dataclass

from leanc.foundation.errors import PatchError
from leanc.repair.patch.model import ADDED, CONTEXT, REMOVED, FilePatch, Hunk, split_lines
from leanc.repair.patch.paths import (
    git_header_paths,
    header_path,
    safe_path,
    strip_git_prefixes,
    unquote_git_path,
)

GIT_DIFF_HEADER = "diff --git "
OLD_FILE_HEADER = "--- "
NEW_FILE_HEADER = "+++ "
HUNK_HEADER_START = "@@"
NO_NEWLINE_MARKER_START = "\\"
OMITTED_HUNK_COUNT = 1
HUNK_BODY_OPS = (ADDED, REMOVED, CONTEXT)

_HUNK_HEADER_RE = re.compile(
    r"^@@\s+-(?P<old_start>\d+)(?:,(?P<old_count>\d+))?\s+\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))?\s+@@(?P<section>.*)$"
)
_BINARY_FILES_RE = re.compile(r"^Binary files (.+) and (.+) differ$")
_IGNORED_GIT_HEADERS = ("index ", "similarity index ", "dissimilarity index ")
_STRIPPED_EMPTY_CONTEXT_LINES = ("\n", "\r\n")


def _is_no_newline_marker(raw: str) -> bool:
    return raw.startswith(NO_NEWLINE_MARKER_START)


def _is_binary_files_line(line: str) -> bool:
    return line.startswith("Binary files ") and line.endswith(" differ")


def _is_header_pair_at(lines: list[str], index: int) -> bool:
    return lines[index].startswith(OLD_FILE_HEADER) and index + 1 < len(lines) and lines[index + 1].startswith(
        NEW_FILE_HEADER
    )


def _hunk_from_header(raw: str, lineno: int) -> Hunk:
    header = raw.rstrip("\r\n")
    match = _HUNK_HEADER_RE.match(header)
    if not match:
        raise PatchError(f"line {lineno}: malformed hunk header {header!r} (expected '@@ -a,b +c,d @@')")
    old_start, new_start = int(match["old_start"]), int(match["new_start"])
    old_count = OMITTED_HUNK_COUNT if match["old_count"] is None else int(match["old_count"])
    new_count = OMITTED_HUNK_COUNT if match["new_count"] is None else int(match["new_count"])
    if old_count > 0 and old_start == 0:
        raise PatchError(f"line {lineno}: hunk header {header!r} starts at line 0 but removes/keeps lines")
    return Hunk(old_start, old_count, new_start, new_count, [], match["section"].strip())


def _mark_no_newline(hunk: Hunk, index: int) -> None:
    if not hunk.lines:
        raise PatchError(f"line {index + 1}: '\\ No newline at end of file' before any hunk line")
    op, text = hunk.lines[-1]
    hunk.lines[-1] = (op, text.removesuffix("\n"))


def _is_context_line_with_stripped_space(raw: str) -> bool:
    return raw in _STRIPPED_EMPTY_CONTEXT_LINES


def _newline_terminated(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def _hunk_line(raw: str) -> tuple[str, str]:
    if _is_context_line_with_stripped_space(raw):
        return CONTEXT, raw
    return raw[:1], _newline_terminated(raw[1:])


def _count_hunk_line(
    hunk: Hunk, op: str, raw: str, index: int, old_remaining: int, new_remaining: int
) -> tuple[int, int]:
    if op == CONTEXT:
        if old_remaining == 0 or new_remaining == 0:
            raise PatchError(f"line {index + 1}: hunk {hunk.header()} has more context lines than its header declares")
        return old_remaining - 1, new_remaining - 1
    if op == REMOVED:
        if old_remaining == 0:
            raise PatchError(f"line {index + 1}: hunk {hunk.header()} removes more lines than its header declares")
        return old_remaining - 1, new_remaining
    if op == ADDED:
        if new_remaining == 0:
            raise PatchError(f"line {index + 1}: hunk {hunk.header()} adds more lines than its header declares")
        return old_remaining, new_remaining - 1
    raise PatchError(
        f"line {index + 1}: unexpected line in hunk {hunk.header()}: {raw.rstrip()!r} "
        f"(expected ' ', '-', '+' or '\\'; {old_remaining} old / {new_remaining} new line(s) still expected)"
    )


def _parse_hunk(lines: list[str], start: int) -> tuple[Hunk, int]:
    hunk = _hunk_from_header(lines[start], start + 1)
    old_remaining, new_remaining = hunk.old_count, hunk.new_count
    index = start + 1
    while old_remaining > 0 or new_remaining > 0:
        if index >= len(lines):
            raise PatchError(
                f"hunk {hunk.header()} is truncated: the patch ended while {old_remaining} old and "
                f"{new_remaining} new line(s) were still expected"
            )
        raw = lines[index]
        if _is_no_newline_marker(raw):
            _mark_no_newline(hunk, index)
            index += 1
            continue
        op, text = _hunk_line(raw)
        old_remaining, new_remaining = _count_hunk_line(hunk, op, raw, index, old_remaining, new_remaining)
        hunk.lines.append((op, text))
        index += 1
    while index < len(lines) and _is_no_newline_marker(lines[index]):
        _mark_no_newline(hunk, index)
        index += 1
    return hunk, index


@dataclass
class _GitBlock:
    lineno: int
    old: str | None = None
    new: str | None = None
    rename_from: str | None = None
    rename_to: str | None = None
    rename_or_copy: bool = False
    binary: bool = False
    mode_change: bool = False
    new_file: bool = False
    deleted_file: bool = False

    def record_extended_header(self, line: str) -> bool:
        if line.startswith(("old mode ", "new mode ")):
            self.mode_change = True
        elif line.startswith("new file mode "):
            self.new_file = True
        elif line.startswith("deleted file mode "):
            self.deleted_file = True
        elif line.startswith(("rename from ", "copy from ")):
            self.rename_or_copy = True
            self.rename_from = unquote_git_path(line.split(" ", 2)[2])
        elif line.startswith(("rename to ", "copy to ")):
            self.rename_or_copy = True
            self.rename_to = unquote_git_path(line.split(" ", 2)[2])
        elif line.startswith("GIT binary patch") or _is_binary_files_line(line):
            self.binary = True
        elif not line.startswith(_IGNORED_GIT_HEADERS):
            return False
        return True

    def copy_flags_to(self, patch: FilePatch) -> None:
        patch.rename_or_copy, patch.binary, patch.mode_change = self.rename_or_copy, self.binary, self.mode_change
        patch.git_new_file, patch.git_deleted_file = self.new_file, self.deleted_file

    def as_headerless_file_patch(self) -> FilePatch:
        old, new = self.old, self.new
        if self.rename_from is not None or self.rename_to is not None:
            old, new = self.rename_from or old, self.rename_to or new
        else:
            old, new = strip_git_prefixes(old, new)
        if self.new_file:
            old = None
        if self.deleted_file:
            new = None
        if old is None and new is None:
            raise PatchError(f"line {self.lineno}: cannot determine the file name of this git patch")
        patch = FilePatch(old_path=safe_path(old, self.lineno), new_path=safe_path(new, self.lineno), hunks=[])
        self.copy_flags_to(patch)
        return patch


class _DiffParser:
    def __init__(self, text: str):
        self.lines = split_lines(text)
        self.index = 0
        self.patches: list[FilePatch] = []
        self.git: _GitBlock | None = None

    def parse(self) -> list[FilePatch]:
        while self.index < len(self.lines):
            self._step(self.lines[self.index].rstrip("\r\n"))
        self._flush_git_block()
        if not self.patches:
            raise PatchError("no file patches found (expected a unified diff with '---'/'+++' headers)")
        return self.patches

    def _step(self, line: str) -> None:
        if line.startswith(GIT_DIFF_HEADER):
            self._start_git_block(line)
        elif self.git is not None and self.git.record_extended_header(line):
            self.index += 1
        elif _is_binary_files_line(line):
            self._binary_file(line)
        elif line.startswith(OLD_FILE_HEADER):
            self._file_patch(line)
        elif line.startswith(NEW_FILE_HEADER):
            raise PatchError(f"line {self.index + 1}: '+++' file header without a preceding '---' header")
        elif line.startswith(HUNK_HEADER_START):
            raise PatchError(f"line {self.index + 1}: hunk header without a preceding '---'/'+++' file header")
        else:
            self.index += 1

    def _flush_git_block(self) -> None:
        if self.git is None:
            return
        block, self.git = self.git, None
        self.patches.append(block.as_headerless_file_patch())

    def _start_git_block(self, line: str) -> None:
        self._flush_git_block()
        self.git = _GitBlock(lineno=self.index + 1)
        self.git.old, self.git.new = git_header_paths(line[len(GIT_DIFF_HEADER) :])
        self.index += 1

    def _binary_file(self, line: str) -> None:
        lineno = self.index + 1
        match = _BINARY_FILES_RE.match(line)
        old, new = strip_git_prefixes(*(map(header_path, match.groups()) if match else (None, None)))
        if old is None and new is None:
            raise PatchError(f"line {lineno}: binary patches are not supported: {line!r}")
        self.patches.append(FilePatch(safe_path(old, lineno), safe_path(new, lineno), [], binary=True))
        self.index += 1

    def _file_patch(self, line: str) -> None:
        lines, index = self.lines, self.index
        if index + 1 >= len(lines) or not lines[index + 1].startswith(NEW_FILE_HEADER):
            raise PatchError(f"line {index + 1}: '---' file header is not followed by a '+++' header")
        old, new = strip_git_prefixes(
            header_path(line[len(OLD_FILE_HEADER) :]), header_path(lines[index + 1][len(NEW_FILE_HEADER) :])
        )
        if old is None and new is None:
            raise PatchError(f"line {index + 1}: both sides of the file header are /dev/null")
        patch = FilePatch(old_path=safe_path(old, index + 1), new_path=safe_path(new, index + 2))
        if self.git is not None:
            self.git.copy_flags_to(patch)
            self.git = None
        self.index = index + 2
        while self.index < len(lines) and lines[self.index].startswith(HUNK_HEADER_START):
            hunk, self.index = _parse_hunk(lines, self.index)
            patch.hunks.append(hunk)
        self._reject_hunk_overrun(patch)
        self.patches.append(patch)

    def _reject_hunk_overrun(self, patch: FilePatch) -> None:
        if self.index >= len(self.lines):
            return
        following = self.lines[self.index]
        if following[:1] in HUNK_BODY_OPS and not _is_header_pair_at(self.lines, self.index) and following.strip():
            raise PatchError(
                f"line {self.index + 1}: unexpected line {following.rstrip()!r} after the last hunk of {patch.path!r} "
                "(hunk is longer than its '@@' header declares?)"
            )


def parse_unified_diff(text: str) -> list[FilePatch]:
    if not isinstance(text, str):
        raise PatchError(f"patch must be a string, got {type(text).__name__}")
    return _DiffParser(text).parse()
