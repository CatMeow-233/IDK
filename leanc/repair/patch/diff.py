"""Deterministic multi-file unified diffs, change statistics and candidate hashes."""

from __future__ import annotations

import difflib

from leanc.foundation.hashing import canonical_hash
from leanc.repair.patch.model import ADDED, CONTEXT, REMOVED, split_lines
from leanc.repair.patch.paths import DEV_NULL, GIT_NEW_PREFIX, GIT_OLD_PREFIX, quote_path

NO_NEWLINE_MARKER = "\\ No newline at end of file"
DEFAULT_CONTEXT_LINES = 3

_UNCHANGED = "equal"
_REMOVES_OLD_LINES = ("replace", "delete")
_ADDS_NEW_LINES = ("replace", "insert")


def _difflib_range(start: int, stop: int) -> str:
    length = stop - start
    beginning = start + 1
    if length == 1:
        return f"{beginning}"
    if not length:
        beginning -= 1
    return f"{beginning},{length}"


def _matcher_without_junk_heuristic(old: list[str], new: list[str]) -> difflib.SequenceMatcher:
    return difflib.SequenceMatcher(None, old, new, autojunk=False)


def _diff_line(op: str, line: str) -> list[str]:
    if line.endswith("\n"):
        return [op + line]
    return [op + line + "\n", NO_NEWLINE_MARKER + "\n"]


def _file_header(prefix: str, git_prefix: str, path: str, content: str | None) -> str:
    return prefix + (DEV_NULL if content is None else quote_path(git_prefix + path)) + "\n"


def _file_diff(path: str, old: str | None, new: str | None, context: int) -> list[str]:
    old_lines = split_lines(old or "")
    new_lines = split_lines(new or "")
    out = [_file_header("--- ", GIT_OLD_PREFIX, path, old), _file_header("+++ ", GIT_NEW_PREFIX, path, new)]
    for group in _matcher_without_junk_heuristic(old_lines, new_lines).get_grouped_opcodes(context):
        first, last = group[0], group[-1]
        out.append(f"@@ -{_difflib_range(first[1], last[2])} +{_difflib_range(first[3], last[4])} @@\n")
        for tag, old_from, old_to, new_from, new_to in group:
            if tag == _UNCHANGED:
                for line in old_lines[old_from:old_to]:
                    out.extend(_diff_line(CONTEXT, line))
                continue
            if tag in _REMOVES_OLD_LINES:
                for line in old_lines[old_from:old_to]:
                    out.extend(_diff_line(REMOVED, line))
            if tag in _ADDS_NEW_LINES:
                for line in new_lines[new_from:new_to]:
                    out.extend(_diff_line(ADDED, line))
    return out


def changed_paths(old: dict[str, str], new: dict[str, str]) -> list[str]:
    """Sorted paths whose content differs (added, removed or modified)."""
    return sorted(p for p in set(old) | set(new) if old.get(p) != new.get(p))


def make_unified_diff(old: dict[str, str], new: dict[str, str], *, context: int = DEFAULT_CONTEXT_LINES) -> str:
    """Sorted, ``a/``/``b/``-prefixed multi-file diff that apply_unified_diff applies back exactly (``""`` if equal)."""
    out: list[str] = []
    for path in changed_paths(old, new):
        out.extend(_file_diff(path, old.get(path), new.get(path), context))
    return "".join(out)


def diff_stats(old: dict[str, str], new: dict[str, str]) -> tuple[int, int]:
    """``(lines_added, lines_removed)`` over all changed files, counted exactly as in make_unified_diff."""
    added = removed = 0
    for path in changed_paths(old, new):
        old_lines = split_lines(old.get(path) or "")
        new_lines = split_lines(new.get(path) or "")
        for tag, old_from, old_to, new_from, new_to in _matcher_without_junk_heuristic(old_lines, new_lines).get_opcodes():
            if tag in _REMOVES_OLD_LINES:
                removed += old_to - old_from
            if tag in _ADDS_NEW_LINES:
                added += new_to - new_from
    return added, removed


def candidate_hash(files: dict[str, str]) -> str:
    """SHA-256 of the canonical JSON of sorted ``[path, content]`` pairs (order-independent, no concatenation games)."""
    return canonical_hash([[path, files[path]] for path in sorted(files)])
