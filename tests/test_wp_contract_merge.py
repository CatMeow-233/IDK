from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from leanc.backends.wp.console import contract_merge_warning
from leanc.cli import main
from leanc.rules.source import source_violations
from leanc.specs.loader import load_spec
from leanc.toolchain.framac import find_framac

REPO = Path(__file__).resolve().parent.parent
EX = REPO / "examples/wp_safe_array_get"
ATTACK = (REPO / "tests/fixtures/integrity/r3_wp_macro_proto.c").read_text()


def _spec_and_headers():
    spec = load_spec(EX / "spec.yaml")
    return spec, {h: (EX / h).read_text() for h in spec.sources.headers}


def test_macro_mentioning_target_is_rejected_lexically():
    spec, headers = _spec_and_headers()
    rel = spec.sources.editable[0]
    v = source_violations(spec, rel, ATTACK, headers=headers)
    assert any(x.rule == "ACSL_CONTRACT" and "macro" in x.message for x in v), v


def test_shipped_wp_sources_still_clean():
    spec, headers = _spec_and_headers()
    rel = spec.sources.editable[0]
    assert source_violations(spec, rel, (EX / rel).read_text(), headers=headers) == []


@pytest.mark.slow
def test_attack_is_not_proved_end_to_end(tmp_path, capsys, needs_toolchain):
    if find_framac() is None:
        pytest.skip("Frama-C not available")
    proj = tmp_path / "p"
    shutil.copytree(EX, proj, ignore=shutil.ignore_patterns(".leanc"))
    spec = load_spec(proj / "spec.yaml")
    (proj / spec.sources.editable[0]).write_text(ATTACK)
    assert main(["spec", "approve", str(proj / "spec.yaml"), "--by", "r", "--yes"]) == 0
    capsys.readouterr()
    rc = main(["verify", str(proj / "spec.yaml"), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["status"] != "PROVED" and rc != 0


def test_wp_merge_warning_regex_matches_frama_c_output():
    text = "[kernel] src/safe_array.c:4: Warning: \n  found two contracts (old location: include/safe_array.h:22-46). Merging them\n"
    assert contract_merge_warning(text)
