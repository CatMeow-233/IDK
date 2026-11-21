from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_template_matches_reference_example():
    tpl = REPO / "leanc/data/templates/safe-array-get"
    ex = REPO / "examples/safe_array_get"
    tpl_files = {p.relative_to(tpl).as_posix() for p in tpl.rglob("*") if p.is_file()}
    ex_files = {p.relative_to(ex).as_posix() for p in ex.rglob("*") if p.is_file() and ".leanc" not in p.parts}
    assert tpl_files == ex_files
    for rel in tpl_files:
        assert (tpl / rel).read_bytes() == (ex / rel).read_bytes(), rel
