from leanc.repair.patch.apply import (
    DEFAULT_MAX_OFFSET,
    apply_file_patches,
    apply_unified_diff,
)
from leanc.repair.patch.diff import (
    candidate_hash,
    changed_paths,
    diff_stats,
    make_unified_diff,
)
from leanc.repair.patch.model import FilePatch, Hunk, split_lines
from leanc.repair.patch.parse import parse_unified_diff
from leanc.repair.patch.proposal import proposal_to_contents

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
