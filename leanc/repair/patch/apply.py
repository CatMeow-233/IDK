"""In-memory, fuzz-free application of parsed file patches (no subprocess, no file-system access)."""

from __future__ import annotations

from collections.abc import Callable

from leanc.foundation.errors import PatchError
from leanc.repair.patch.model import FilePatch, Hunk, split_lines
from leanc.repair.patch.parse import parse_unified_diff

DEFAULT_MAX_OFFSET = 50
EXCERPT_LINES = 6


def _excerpt(lines: list[str], limit: int = EXCERPT_LINES) -> str:
    shown = [repr(line.rstrip("\n")) for line in lines[:limit]]
    if len(lines) > limit:
        shown.append(f"... ({len(lines) - limit} more)")
    return "\n    ".join(shown) if shown else "(nothing)"


def _declared_index(hunk: Hunk) -> int:
    return hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1


def _search_order(wanted: int, max_offset: int):
    for distance in range(max_offset + 1):
        yield from (wanted,) if distance == 0 else (wanted - distance, wanted + distance)


def _matches_at(source: list[str], expected: list[str], position: int, earliest: int) -> bool:
    end = position + len(expected)
    return earliest <= position and end <= len(source) and source[position:end] == expected


def _exact_insertion_point(source: list[str], wanted: int, earliest: int) -> int | None:
    return wanted if earliest <= wanted <= len(source) else None


def _locate_hunk(source: list[str], expected: list[str], wanted: int, earliest: int, max_offset: int) -> int | None:
    if not expected:
        return _exact_insertion_point(source, wanted, earliest)
    return next((p for p in _search_order(wanted, max_offset) if _matches_at(source, expected, p, earliest)), None)


def _hunk_mismatch(patch: FilePatch, number: int, hunk: Hunk, source: list[str], wanted: int, max_offset: int) -> PatchError:
    expected = hunk.old_lines()
    shown_from = max(wanted, 0)
    actual = source[shown_from : shown_from + max(len(expected), 1)]
    return PatchError(
        f"{patch.path}: hunk #{number} {hunk.header()} does not apply: the context/removed lines were not found "
        f"at line {wanted + 1} or within {max_offset} lines of it (exact match required).\n"
        f"  expected:\n    {_excerpt(expected)}\n  found at line {wanted + 1}:\n    {_excerpt(actual)}"
    )


def _reject_missing_newline_before_end(patch: FilePatch, result: list[str]) -> None:
    for index, line in enumerate(result[:-1]):
        if not line.endswith("\n"):
            raise PatchError(
                f"{patch.path}: '\\ No newline at end of file' applies to a line that would not be the last "
                f"line of the result (line {index + 1}: {line!r})"
            )


def _apply_hunks(patch: FilePatch, base: str, max_offset: int) -> str:
    source = split_lines(base)
    result: list[str] = []
    first_uncopied = 0
    drift_of_previous_hunk = 0
    for number, hunk in enumerate(patch.hunks, 1):
        expected = hunk.old_lines()
        declared = _declared_index(hunk)
        wanted = declared + drift_of_previous_hunk
        found = _locate_hunk(source, expected, wanted, first_uncopied, max_offset)
        if found is None:
            raise _hunk_mismatch(patch, number, hunk, source, wanted, max_offset)
        result.extend(source[first_uncopied:found])
        result.extend(hunk.new_lines())
        first_uncopied = found + len(expected)
        drift_of_previous_hunk = found - declared
    result.extend(source[first_uncopied:])
    _reject_missing_newline_before_end(patch, result)
    return "".join(result)


def _changes_path(patch: FilePatch) -> bool:
    return patch.old_path is not None and patch.new_path is not None and patch.old_path != patch.new_path


def _reject_unsupported(patch: FilePatch) -> None:
    if patch.binary:
        raise PatchError(f"{patch.path}: binary patches are not supported")
    if patch.is_deletion or patch.git_deleted_file:
        raise PatchError(f"{patch.path}: deleting files is not allowed in a repair patch")
    if patch.rename_or_copy or _changes_path(patch):
        raise PatchError(
            f"renaming/copying files is not allowed in a repair patch ({patch.old_path!r} -> {patch.new_path!r})"
        )
    if patch.mode_change:
        raise PatchError(f"{patch.path}: file mode changes are not supported")
    if not patch.hunks and not patch.is_new_file:
        raise PatchError(f"{patch.path}: file patch contains no hunks")


def _content_before(patch: FilePatch, patched_so_far: dict[str, str], read_file: Callable[[str], str | None]) -> str:
    path = patch.path
    base = patched_so_far[path] if path in patched_so_far else read_file(path)
    if patch.is_new_file:
        if base not in (None, ""):
            raise PatchError(f"{path}: patch creates this file (--- /dev/null) but it already exists")
        return ""
    if base is None:
        raise PatchError(f"{path}: patch modifies a file that does not exist (or is not available to patch)")
    return base


def apply_file_patches(
    patches: list[FilePatch],
    read_file: Callable[[str], str | None],
    *,
    max_offset: int = DEFAULT_MAX_OFFSET,
) -> dict[str, str]:
    """Apply already-parsed file patches in order (several patches of one file apply in sequence)."""
    results: dict[str, str] = {}
    for patch in patches:
        _reject_unsupported(patch)
        base = _content_before(patch, results, read_file)
        results[patch.path] = _apply_hunks(patch, base, max_offset)
    return results


def apply_unified_diff(
    text: str,
    read_file: Callable[[str], str | None],
    *,
    max_offset: int = DEFAULT_MAX_OFFSET,
) -> dict[str, str]:
    """New content of every path a unified diff touches; exact context within ``max_offset`` lines, else PatchError."""
    return apply_file_patches(parse_unified_diff(text), read_file, max_offset=max_offset)
