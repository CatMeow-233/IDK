"""Unified-diff handling for repair proposals (pure Python, no ``patch`` binary).

A repair provider proposes either *full file contents* or a *unified diff*.
Both are turned into a complete candidate content map by
:func:`proposal_to_contents`; the repair loop then diffs the candidate against
the current sources (:func:`make_unified_diff`, :func:`diff_stats`) and hands
it to :mod:`leanc.repair.policy`.

Why our own applier instead of GNU ``patch``:

* no subprocess and no file-system access: the patch is applied to in-memory
  strings, so a hostile diff cannot write outside the workspace, follow
  symlinks or touch protected files - it can only produce a content map that
  the policy then inspects;
* deliberately strict semantics: context must match exactly (fuzz 0) within a
  small line-offset window, hunk line counts must agree with their headers,
  and deletions, renames, copies, mode changes and binary patches are refused
  instead of being half-applied or silently skipped.

Paths inside a diff are validated with
:func:`leanc.core.workspace.normalize_relpath`; an absolute or ``..`` path is
a :class:`~leanc.core.errors.PatchError`, never something passed to a reader.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from leanc.core.errors import PatchError, WorkspaceError
from leanc.core.hashing import canonical_hash
from leanc.core.workspace import normalize_relpath

__all__ = [
    "DEFAULT_MAX_OFFSET",
    "FilePatch",
    "Hunk",
    "apply_file_patches",
    "apply_unified_diff",
    "candidate_hash",
    "changed_paths",
    "diff_stats",
    "make_unified_diff",
    "parse_unified_diff",
    "proposal_to_contents",
    "split_lines",
]

#: How far (in lines, either direction) a hunk may be found from the position
#: its header claims.  Small on purpose: a larger window makes it more likely
#: that repetitive C code (``}``, ``return 0;``) matches in the wrong place.
DEFAULT_MAX_OFFSET = 50

_NO_NEWLINE = "\\ No newline at end of file"
_HUNK_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(.*)$")
_GIT_SIMPLE_HEADERS = (
    "index ",
    "similarity index ",
    "dissimilarity index ",
)


# --------------------------------------------------------------------------- data
@dataclass
class Hunk:
    """One ``@@ -a,b +c,d @@`` hunk.

    ``lines`` holds ``(op, text)`` pairs where ``op`` is ``' '`` (context),
    ``'-'`` (removed) or ``'+'`` (added) and ``text`` includes its trailing
    ``"\\n"`` unless the diff marked it with ``\\ No newline at end of file``.
    """

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[str, str]] = field(default_factory=list)
    section: str = ""  # optional text after the closing "@@" (function name)

    def old_lines(self) -> list[str]:
        return [t for op, t in self.lines if op in (" ", "-")]

    def new_lines(self) -> list[str]:
        return [t for op, t in self.lines if op in (" ", "+")]

    def header(self) -> str:
        return f"@@ -{self.old_start},{self.old_count} +{self.new_start},{self.new_count} @@"


@dataclass
class FilePatch:
    """All hunks for one file.  ``old_path`` is ``None`` for ``/dev/null``
    (file creation), ``new_path`` is ``None`` for a deletion.  Paths are
    normalised workspace-relative POSIX paths with ``a/`` / ``b/`` stripped."""

    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)
    rename: bool = False  # git "rename from/to" or "copy from/to"
    binary: bool = False  # "Binary files ... differ" / "GIT binary patch"
    mode_change: bool = False  # git "old mode"/"new mode"
    git_new_file: bool = False  # git "new file mode"
    git_deleted_file: bool = False  # git "deleted file mode"

    @property
    def path(self) -> str:
        """The path this patch writes (the old path for a deletion)."""
        p = self.new_path if self.new_path is not None else self.old_path
        assert p is not None  # parse_unified_diff never yields /dev/null on both sides
        return p

    @property
    def is_new_file(self) -> bool:
        return self.old_path is None

    @property
    def is_deletion(self) -> bool:
        return self.new_path is None


# --------------------------------------------------------------------------- helpers
def split_lines(text: str) -> list[str]:
    """Split on ``"\\n"`` only, keeping the terminator.

    ``str.splitlines`` also splits on ``\\r``, form feeds, ``\\x1c`` ... which
    occur in real C files; treating them as line breaks would make diffs that
    other tools cannot apply.  A final line without ``"\\n"`` is kept as is.
    """
    if not text:
        return []
    parts = text.split("\n")
    out = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def _unquote_git_path(s: str) -> str:
    """Undo git's C-style quoting of unusual path names (``"a/x\\ty"``)."""
    if len(s) < 2 or not (s.startswith('"') and s.endswith('"')):
        return s
    body = s[1:-1]
    out = bytearray()
    i = 0
    simple = {"n": b"\n", "t": b"\t", "r": b"\r", '"': b'"', "\\": b"\\", "a": b"\a", "b": b"\b", "f": b"\f", "v": b"\v"}
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in simple:
                out += simple[nxt]
                i += 2
                continue
            if re.match(r"[0-7]{3}", body[i + 1 : i + 4]):
                out.append(int(body[i + 1 : i + 4], 8) & 0xFF)
                i += 4
                continue
        out += ch.encode("utf-8")
        i += 1
    return out.decode("utf-8", errors="replace")


def _quote_path(p: str) -> str:
    if any(c in p for c in '\t\n\r"\\') or any(ord(c) < 32 for c in p):
        esc = p.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
        return f'"{esc}"'
    return p


def _header_path(raw: str) -> str | None:
    """Path from a ``---``/``+++`` line (timestamp after a TAB is dropped)."""
    s = raw.rstrip("\r\n")
    if not s.startswith('"') and "\t" in s:
        s = s.split("\t", 1)[0]
    s = s.strip()
    s = _unquote_git_path(s)
    if s == "/dev/null":
        return None
    return s


def _strip_prefixes(old: str | None, new: str | None) -> tuple[str | None, str | None]:
    """Strip git's ``a/`` and ``b/`` prefixes when the diff uses them (both
    sides, or the one side that is not ``/dev/null``).  Diffs without the
    prefixes (``-p0`` style) are used verbatim."""
    old_ok = old is None or old.startswith("a/")
    new_ok = new is None or new.startswith("b/")
    if old_ok and new_ok:
        return (None if old is None else old[2:], None if new is None else new[2:])
    return old, new


def _safe_path(p: str | None, lineno: int) -> str | None:
    if p is None:
        return None
    try:
        return normalize_relpath(p)
    except WorkspaceError as exc:
        raise PatchError(f"line {lineno}: unsafe path in patch: {exc}") from None


def _git_header_paths(rest: str) -> tuple[str | None, str | None]:
    """Best-effort paths from ``diff --git a/X b/Y`` (only needed for git
    patches without ``---``/``+++`` lines, e.g. empty new files)."""
    rest = rest.rstrip("\r\n")
    m = re.match(r'^("(?:[^"\\]|\\.)*"|\S+) ("(?:[^"\\]|\\.)*"|\S+)$', rest)
    if m:
        return _unquote_git_path(m.group(1)), _unquote_git_path(m.group(2))
    # unquoted names containing spaces: "a/NAME b/NAME" with identical NAMEs
    if rest.startswith("a/") and (len(rest) - 5) % 2 == 0:
        k = (len(rest) - 5) // 2
        name = rest[2 : 2 + k]
        if rest == f"a/{name} b/{name}":
            return f"a/{name}", f"b/{name}"
    return None, None


# --------------------------------------------------------------------------- parsing
@dataclass
class _GitMeta:
    lineno: int
    old: str | None = None
    new: str | None = None
    rename_from: str | None = None
    rename_to: str | None = None
    rename: bool = False
    binary: bool = False
    mode_change: bool = False
    new_file: bool = False
    deleted_file: bool = False


def _parse_hunk(lines: list[str], i: int) -> tuple[Hunk, int]:
    header = lines[i].rstrip("\r\n")
    m = _HUNK_RE.match(header)
    if not m:
        raise PatchError(f"line {i + 1}: malformed hunk header {header!r} (expected '@@ -a,b +c,d @@')")
    old_start, new_start = int(m.group(1)), int(m.group(3))
    old_count = 1 if m.group(2) is None else int(m.group(2))
    new_count = 1 if m.group(4) is None else int(m.group(4))
    if old_count > 0 and old_start == 0:
        raise PatchError(f"line {i + 1}: hunk header {header!r} starts at line 0 but removes/keeps lines")
    hunk = Hunk(old_start, old_count, new_start, new_count, [], m.group(5).strip())
    old_rem, new_rem = old_count, new_count
    i += 1
    n = len(lines)

    def _mark_no_newline(at: int) -> None:
        if not hunk.lines:
            raise PatchError(f"line {at + 1}: '\\ No newline at end of file' before any hunk line")
        op, text = hunk.lines[-1]
        hunk.lines[-1] = (op, text.removesuffix("\n"))

    while old_rem > 0 or new_rem > 0:
        if i >= n:
            raise PatchError(
                f"hunk {hunk.header()} is truncated: the patch ended while {old_rem} old and "
                f"{new_rem} new line(s) were still expected"
            )
        raw = lines[i]
        if raw.startswith("\\"):
            _mark_no_newline(i)
            i += 1
            continue
        op = raw[:1]
        text = raw[1:]
        if raw in ("\n", "\r\n"):
            # an entirely empty line inside a hunk: an empty context line whose
            # leading space was stripped (common in hand/LLM-written diffs).
            op, text = " ", raw
        if not text.endswith("\n"):
            text += "\n"  # last line of the diff text itself; only "\" markers remove newlines
        if op == " ":
            if old_rem == 0 or new_rem == 0:
                raise PatchError(
                    f"line {i + 1}: hunk {hunk.header()} has more context lines than its header declares"
                )
            old_rem -= 1
            new_rem -= 1
        elif op == "-":
            if old_rem == 0:
                raise PatchError(f"line {i + 1}: hunk {hunk.header()} removes more lines than its header declares")
            old_rem -= 1
        elif op == "+":
            if new_rem == 0:
                raise PatchError(f"line {i + 1}: hunk {hunk.header()} adds more lines than its header declares")
            new_rem -= 1
        else:
            raise PatchError(
                f"line {i + 1}: unexpected line in hunk {hunk.header()}: {raw.rstrip()!r} "
                f"(expected ' ', '-', '+' or '\\'; {old_rem} old / {new_rem} new line(s) still expected)"
            )
        hunk.lines.append((op, text))
        i += 1
    while i < n and lines[i].startswith("\\"):
        _mark_no_newline(i)
        i += 1
    return hunk, i


def parse_unified_diff(text: str) -> list[FilePatch]:
    """Parse a (possibly multi-file, possibly git-style) unified diff.

    Supports ``a/``/``b/`` prefixes, ``/dev/null`` for created files, hunk
    headers with omitted counts (``@@ -3 +3 @@``), ``\\ No newline at end of
    file`` markers, git extended headers and leading/trailing prose (e.g. a
    Markdown code fence around the diff).  Hunk bodies are read by their
    declared line counts, so a removed line that itself starts with ``--``
    is not mistaken for a file header.  Raises :class:`PatchError` on any
    malformed or truncated input, or on unsafe (absolute / ``..``) paths.
    """
    if not isinstance(text, str):
        raise PatchError(f"patch must be a string, got {type(text).__name__}")
    lines = split_lines(text)
    n = len(lines)
    patches: list[FilePatch] = []
    git: _GitMeta | None = None

    def flush_git() -> None:
        """A ``diff --git`` block that had no ``---``/``+++`` lines."""
        nonlocal git
        if git is None:
            return
        g, git = git, None
        old, new = g.old, g.new
        if g.rename_from is not None or g.rename_to is not None:
            old, new = g.rename_from or old, g.rename_to or new
        else:
            old, new = _strip_prefixes(old, new)
        if g.new_file:
            old = None
        if g.deleted_file:
            new = None
        if old is None and new is None:
            raise PatchError(f"line {g.lineno}: cannot determine the file name of this git patch")
        patches.append(
            FilePatch(
                old_path=_safe_path(old, g.lineno),
                new_path=_safe_path(new, g.lineno),
                hunks=[],
                rename=g.rename,
                binary=g.binary,
                mode_change=g.mode_change,
                git_new_file=g.new_file,
                git_deleted_file=g.deleted_file,
            )
        )

    i = 0
    while i < n:
        line = lines[i].rstrip("\r\n")
        if line.startswith("diff --git "):
            flush_git()
            git = _GitMeta(lineno=i + 1)
            git.old, git.new = _git_header_paths(line[len("diff --git ") :])
            i += 1
            continue
        if git is not None:
            handled = True
            if line.startswith(("old mode ", "new mode ")):
                git.mode_change = True
            elif line.startswith("new file mode "):
                git.new_file = True
            elif line.startswith("deleted file mode "):
                git.deleted_file = True
            elif line.startswith(("rename from ", "copy from ")):
                git.rename = True
                git.rename_from = _unquote_git_path(line.split(" ", 2)[2])
            elif line.startswith(("rename to ", "copy to ")):
                git.rename = True
                git.rename_to = _unquote_git_path(line.split(" ", 2)[2])
            elif line.startswith("GIT binary patch") or (line.startswith("Binary files ") and line.endswith(" differ")):
                git.binary = True
            elif line.startswith(_GIT_SIMPLE_HEADERS):
                pass
            else:
                handled = False
            if handled:
                i += 1
                continue
        if line.startswith("Binary files ") and line.endswith(" differ"):
            m = re.match(r"^Binary files (.+) and (.+) differ$", line)
            old, new = _strip_prefixes(*(map(_header_path, m.groups()) if m else (None, None)))
            if old is None and new is None:
                raise PatchError(f"line {i + 1}: binary patches are not supported: {line!r}")
            patches.append(FilePatch(_safe_path(old, i + 1), _safe_path(new, i + 1), [], binary=True))
            i += 1
            continue
        if line.startswith("--- "):
            if i + 1 >= n or not lines[i + 1].startswith("+++ "):
                raise PatchError(f"line {i + 1}: '---' file header is not followed by a '+++' header")
            old, new = _strip_prefixes(_header_path(line[4:]), _header_path(lines[i + 1][4:]))
            if old is None and new is None:
                raise PatchError(f"line {i + 1}: both sides of the file header are /dev/null")
            fp = FilePatch(old_path=_safe_path(old, i + 1), new_path=_safe_path(new, i + 2))
            if git is not None:
                fp.rename, fp.binary, fp.mode_change = git.rename, git.binary, git.mode_change
                fp.git_new_file, fp.git_deleted_file = git.new_file, git.deleted_file
                git = None
            i += 2
            while i < n and lines[i].startswith("@@"):
                hunk, i = _parse_hunk(lines, i)
                fp.hunks.append(hunk)
            if i < n:
                nxt = lines[i]
                is_header_pair = nxt.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ ")
                if nxt[:1] in ("+", "-", " ") and not is_header_pair and nxt.strip():
                    raise PatchError(
                        f"line {i + 1}: unexpected line {nxt.rstrip()!r} after the last hunk of {fp.path!r} "
                        "(hunk is longer than its '@@' header declares?)"
                    )
            patches.append(fp)
            continue
        if line.startswith("+++ "):
            raise PatchError(f"line {i + 1}: '+++' file header without a preceding '---' header")
        if line.startswith("@@"):
            raise PatchError(f"line {i + 1}: hunk header without a preceding '---'/'+++' file header")
        i += 1  # prose, code fences, "Only in ..." etc. carry no content changes
    flush_git()
    if not patches:
        raise PatchError("no file patches found (expected a unified diff with '---'/'+++' headers)")
    return patches


# --------------------------------------------------------------------------- applying
def _excerpt(lines: list[str], limit: int = 6) -> str:
    shown = [repr(x.rstrip("\n")) for x in lines[:limit]]
    if len(lines) > limit:
        shown.append(f"... ({len(lines) - limit} more)")
    return "\n    ".join(shown) if shown else "(nothing)"


def _apply_hunks(fp: FilePatch, base: str, max_offset: int) -> str:
    src = split_lines(base)
    out: list[str] = []
    pos = 0  # first source line not yet copied
    drift = 0  # offset at which the previous hunk was actually found
    for k, h in enumerate(fp.hunks, 1):
        old = h.old_lines()
        new = h.new_lines()
        declared = h.old_start if h.old_count == 0 else h.old_start - 1
        want = declared + drift
        found: int | None = None
        if not old:
            # pure insertion without context: there is nothing to match, so it
            # can only go exactly where the header says.
            if pos <= want <= len(src):
                found = want
        else:
            for d in range(max_offset + 1):
                for p in ((want,) if d == 0 else (want - d, want + d)):
                    if pos <= p and p + len(old) <= len(src) and src[p : p + len(old)] == old:
                        found = p
                        break
                if found is not None:
                    break
        if found is None:
            actual = src[max(want, 0) : max(want, 0) + max(len(old), 1)]
            raise PatchError(
                f"{fp.path}: hunk #{k} {h.header()} does not apply: the context/removed lines were not found "
                f"at line {want + 1} or within {max_offset} lines of it (exact match required).\n"
                f"  expected:\n    {_excerpt(old)}\n  found at line {want + 1}:\n    {_excerpt(actual)}"
            )
        out.extend(src[pos:found])
        out.extend(new)
        pos = found + len(old)
        drift = found - declared
    out.extend(src[pos:])
    for idx, line in enumerate(out[:-1]):
        if not line.endswith("\n"):
            raise PatchError(
                f"{fp.path}: '\\ No newline at end of file' applies to a line that would not be the last "
                f"line of the result (line {idx + 1}: {line!r})"
            )
    return "".join(out)


def _reject_unsupported(fp: FilePatch) -> None:
    if fp.binary:
        raise PatchError(f"{fp.path}: binary patches are not supported")
    if fp.is_deletion or fp.git_deleted_file:
        raise PatchError(f"{fp.path}: deleting files is not allowed in a repair patch")
    if fp.rename or (fp.old_path is not None and fp.new_path is not None and fp.old_path != fp.new_path):
        raise PatchError(
            f"renaming/copying files is not allowed in a repair patch ({fp.old_path!r} -> {fp.new_path!r})"
        )
    if fp.mode_change:
        raise PatchError(f"{fp.path}: file mode changes are not supported")
    if not fp.hunks and not fp.is_new_file:
        raise PatchError(f"{fp.path}: file patch contains no hunks")


def apply_file_patches(
    patches: list[FilePatch],
    read_file: Callable[[str], str | None],
    *,
    max_offset: int = DEFAULT_MAX_OFFSET,
) -> dict[str, str]:
    """Apply already-parsed file patches; see :func:`apply_unified_diff`."""
    results: dict[str, str] = {}
    for fp in patches:
        _reject_unsupported(fp)
        path = fp.path
        if path in results:
            base: str | None = results[path]  # several patches for one file apply in sequence
        else:
            base = read_file(path)
        if fp.is_new_file:
            if base not in (None, ""):
                raise PatchError(f"{path}: patch creates this file (--- /dev/null) but it already exists")
            base = ""
        elif base is None:
            raise PatchError(f"{path}: patch modifies a file that does not exist (or is not available to patch)")
        results[path] = _apply_hunks(fp, base, max_offset)
    return results


def apply_unified_diff(
    text: str,
    read_file: Callable[[str], str | None],
    *,
    max_offset: int = DEFAULT_MAX_OFFSET,
) -> dict[str, str]:
    """Apply a unified diff to in-memory files.

    ``read_file(path)`` returns the current content of a (normalised,
    workspace-relative) path or ``None`` if it does not exist.  Returns the
    new content of every touched path.  Context and removed lines must match
    exactly (fuzz 0); a hunk may be found up to ``max_offset`` lines away
    from its declared position (nearest match wins) but never before the end
    of the previous hunk.  Raises :class:`PatchError` on malformed input,
    mismatched context, creation of an existing file, modification of a
    missing file, and on deletions, renames, copies, mode changes or binary
    patches, which a repair is never allowed to perform.
    """
    return apply_file_patches(parse_unified_diff(text), read_file, max_offset=max_offset)


def proposal_to_contents(
    files: dict[str, str] | None,
    patch: str | None,
    current: dict[str, str],
) -> dict[str, str]:
    """Turn a repair proposal into a full candidate content map.

    Exactly one of ``files`` (full new contents) and ``patch`` (unified diff)
    must be non-empty, otherwise ``PatchError("empty proposal")`` (or an
    "ambiguous proposal" error when both are given).  The result is
    ``current`` updated with the proposed changes; it is *not* policy-checked
    here - that is :func:`leanc.repair.policy.check_candidate`'s job, so:

    * ``files`` keys are normalised when they are valid relative paths; an
      invalid key (absolute, ``..``) is kept verbatim so the policy reports
      ``PATH_INVALID``, and a valid key outside ``current`` (e.g. the harness
      or a new file) is added so the policy reports ``PATH_NOT_EDITABLE``;
    * with ``patch``, ``current`` is the only thing the diff can be applied
      to.  Creating a file (``--- /dev/null``) yields a candidate containing
      that path (the policy then decides whether it is editable).  Modifying
      a file that is not in ``current`` - in the repair loop ``current`` holds
      only the editable sources, so this is how an attempt to patch the
      harness, test driver, headers or spec shows up - raises
      :class:`PatchError` naming the file, because its original content is
      not available to apply the hunks to.  The loop records that as
      ``PATCH_INVALID``.
    """
    has_files = bool(files)
    has_patch = isinstance(patch, str) and bool(patch.strip())
    if patch is not None and not isinstance(patch, str):
        raise PatchError(f"patch must be a string, got {type(patch).__name__}")
    if has_files and has_patch:
        raise PatchError("ambiguous proposal: it contains both full files and a patch (exactly one is allowed)")
    if not has_files and not has_patch:
        raise PatchError("empty proposal")
    out = dict(current)
    if has_files:
        if not isinstance(files, dict):
            raise PatchError(f"proposal 'files' must be a mapping of path -> content, got {type(files).__name__}")
        origin: dict[str, str] = {}
        for raw, content in files.items():
            if not isinstance(raw, str) or not isinstance(content, str):
                raise PatchError(f"proposal 'files' must map path strings to content strings (bad entry {raw!r})")
            try:
                key = normalize_relpath(raw)
            except WorkspaceError:
                key = raw  # kept verbatim: the policy reports PATH_INVALID
            if key in origin:
                raise PatchError(f"proposal lists {origin[key]!r} and {raw!r}, which name the same file")
            origin[key] = raw
            out[key] = content
        return out
    assert isinstance(patch, str)
    parsed = parse_unified_diff(patch)
    for fp in parsed:
        if fp.is_new_file or fp.is_deletion or fp.rename or fp.binary or fp.old_path != fp.new_path:
            continue  # creation is checked by the policy; the rest is refused by the applier
        target = fp.path
        if target not in current:
            raise PatchError(
                f"patch modifies {target!r}, which is not one of the editable sources being repaired "
                f"({', '.join(sorted(current)) or 'none'}); protected files (harness, test driver, headers, "
                "spec, approvals) and other files cannot be changed by a repair"
            )
    out.update(apply_file_patches(parsed, current.get))
    return out


# --------------------------------------------------------------------------- diffing
def _range(start: int, stop: int) -> str:
    """Unified-diff range in the same convention as :mod:`difflib`."""
    length = stop - start
    beginning = start + 1
    if length == 1:
        return f"{beginning}"
    if not length:
        beginning -= 1
    return f"{beginning},{length}"


def _matcher(a: list[str], b: list[str]) -> difflib.SequenceMatcher:
    # autojunk=False: with the default heuristic, frequent lines ("}", blank
    # lines) in files over 200 lines are ignored as junk, which yields
    # needlessly large diffs and inflates the changed-lines budget.
    return difflib.SequenceMatcher(None, a, b, autojunk=False)


def _file_diff(path: str, old: str | None, new: str | None, context: int) -> list[str]:
    a = split_lines(old or "")
    b = split_lines(new or "")
    out = [
        "--- " + ("/dev/null" if old is None else _quote_path("a/" + path)) + "\n",
        "+++ " + ("/dev/null" if new is None else _quote_path("b/" + path)) + "\n",
    ]

    def emit(op: str, line: str) -> None:
        if line.endswith("\n"):
            out.append(op + line)
        else:
            out.append(op + line + "\n")
            out.append(_NO_NEWLINE + "\n")

    for group in _matcher(a, b).get_grouped_opcodes(context):
        first, last = group[0], group[-1]
        out.append(f"@@ -{_range(first[1], last[2])} +{_range(first[3], last[4])} @@\n")
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for line in a[i1:i2]:
                    emit(" ", line)
                continue
            if tag in ("replace", "delete"):
                for line in a[i1:i2]:
                    emit("-", line)
            if tag in ("replace", "insert"):
                for line in b[j1:j2]:
                    emit("+", line)
    return out


def changed_paths(old: dict[str, str], new: dict[str, str]) -> list[str]:
    """Sorted paths whose content differs (added, removed or modified)."""
    return sorted(p for p in set(old) | set(new) if old.get(p) != new.get(p))


def make_unified_diff(old: dict[str, str], new: dict[str, str], *, context: int = 3) -> str:
    """Deterministic multi-file unified diff (sorted paths, ``a/``/``b/``
    prefixes, ``/dev/null`` for added/removed files, ``\\ No newline at end
    of file`` markers) that :func:`apply_unified_diff` applies back exactly.
    Returns ``""`` when nothing changed."""
    out: list[str] = []
    for p in changed_paths(old, new):
        out.extend(_file_diff(p, old.get(p), new.get(p), context))
    return "".join(out)


def diff_stats(old: dict[str, str], new: dict[str, str]) -> tuple[int, int]:
    """``(lines_added, lines_removed)`` over all changed files, counted
    exactly as in :func:`make_unified_diff`'s output."""
    added = removed = 0
    for p in changed_paths(old, new):
        a = split_lines(old.get(p) or "")
        b = split_lines(new.get(p) or "")
        for tag, i1, i2, j1, j2 in _matcher(a, b).get_opcodes():
            if tag in ("replace", "delete"):
                removed += i2 - i1
            if tag in ("replace", "insert"):
                added += j2 - j1
    return added, removed


def candidate_hash(files: dict[str, str]) -> str:
    """SHA-256 over the canonical JSON of the sorted ``[path, content]``
    pairs: equal content maps hash equally regardless of insertion order,
    and the encoding is unambiguous (no path/content concatenation games)."""
    return canonical_hash([[p, files[p]] for p in sorted(files)])
