"""Turn a repair proposal (full files or a unified diff) into a complete candidate content map for the policy."""

from __future__ import annotations

from leanc.foundation.errors import PatchError, WorkspaceError
from leanc.foundation.workspace import normalize_relpath
from leanc.repair.patch.apply import apply_file_patches
from leanc.repair.patch.model import FilePatch
from leanc.repair.patch.parse import parse_unified_diff


def _key_the_policy_can_judge(raw: str) -> str:
    try:
        return normalize_relpath(raw)
    except WorkspaceError:
        return raw


def _with_full_files(files: object, current: dict[str, str]) -> dict[str, str]:
    if not isinstance(files, dict):
        raise PatchError(f"proposal 'files' must be a mapping of path -> content, got {type(files).__name__}")
    candidate = dict(current)
    spelled_as: dict[str, str] = {}
    for raw, content in files.items():
        if not isinstance(raw, str) or not isinstance(content, str):
            raise PatchError(f"proposal 'files' must map path strings to content strings (bad entry {raw!r})")
        key = _key_the_policy_can_judge(raw)
        if key in spelled_as:
            raise PatchError(f"proposal lists {spelled_as[key]!r} and {raw!r}, which name the same file")
        spelled_as[key] = raw
        candidate[key] = content
    return candidate


def _is_in_place_modification(patch: FilePatch) -> bool:
    return not (
        patch.is_new_file
        or patch.is_deletion
        or patch.rename_or_copy
        or patch.binary
        or patch.old_path != patch.new_path
    )


def _reject_patching_unavailable_files(patches: list[FilePatch], current: dict[str, str]) -> None:
    for patch in patches:
        if _is_in_place_modification(patch) and patch.path not in current:
            raise PatchError(
                f"patch modifies {patch.path!r}, which is not one of the editable sources being repaired "
                f"({', '.join(sorted(current)) or 'none'}); protected files (harness, test driver, headers, "
                "spec, approvals) and other files cannot be changed by a repair"
            )


def _with_patch(patch: str, current: dict[str, str]) -> dict[str, str]:
    parsed = parse_unified_diff(patch)
    _reject_patching_unavailable_files(parsed, current)
    candidate = dict(current)
    candidate.update(apply_file_patches(parsed, current.get))
    return candidate


def proposal_to_contents(
    files: dict[str, str] | None,
    patch: str | None,
    current: dict[str, str],
) -> dict[str, str]:
    """``current`` updated with exactly one of ``files`` / ``patch``; not policy-checked (that is check_candidate's job)."""
    has_files = bool(files)
    has_patch = isinstance(patch, str) and bool(patch.strip())
    if patch is not None and not isinstance(patch, str):
        raise PatchError(f"patch must be a string, got {type(patch).__name__}")
    if has_files and has_patch:
        raise PatchError("ambiguous proposal: it contains both full files and a patch (exactly one is allowed)")
    if not has_files and not has_patch:
        raise PatchError("empty proposal")
    if has_files:
        return _with_full_files(files, current)
    return _with_patch(patch, current)
