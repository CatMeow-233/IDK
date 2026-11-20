"""Every example under examples/ must produce exactly the statuses declared in
its expected.yaml, both for `leanc verify` on the shipped sources and for the
full `leanc run --provider mock` workflow (mock generation + repair)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from leanc.cli import main

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
CASES = sorted(p.parent.name for p in EXAMPLES.glob("*/expected.yaml"))


def _cli(capsys, *argv) -> tuple[int, dict]:
    rc = main([*argv, "--json"])
    return rc, json.loads(capsys.readouterr().out)


def _fresh(name: str, dst: Path) -> Path:
    shutil.copytree(EXAMPLES / name, dst, ignore=shutil.ignore_patterns(".leanc"))
    return dst


def _approve(capsys, spec: Path) -> None:
    rc, data = _cli(capsys, "spec", "approve", str(spec), "--by", "e2e-reviewer", "--yes")
    assert rc == 0, data


def _tools(payload: dict) -> set[str]:
    return {f["tool"] for f in payload.get("verification", {}).get("failures", [])}


def _skip_without_v2_tools(name: str) -> None:
    """V2 gates need optional tools; skip (never fail) when they are absent."""
    from leanc.specs.loader import load_spec

    spec = load_spec(EXAMPLES / name / "spec.yaml")
    if "wp" in spec.gates:
        from leanc.backends.wp import MIN_FRAMAC_MAJOR, find_framac, framac_version_key
        from leanc.backends.tools import tool_version

        fc = find_framac()
        if fc is None or framac_version_key(tool_version(fc.real, "-version"))[0] < MIN_FRAMAC_MAJOR:
            pytest.skip("Frama-C/WP (>= 27) not available")
    if "lean" in spec.gates:
        from leanc.backends.lean import find_lean

        lt = find_lean()
        if lt is None or not lt.leanchecker:
            pytest.skip("Lean 4 toolchain with leanchecker not available")
    if spec.language.is_cpp:
        from leanc.backends.cxx import find_clangxx
        from leanc.backends.tools import find_clang

        if find_clangxx(find_clang()) is None:
            pytest.skip("clang++ not available")


@pytest.mark.slow
@pytest.mark.parametrize("name", CASES)
def test_example_statuses(name, tmp_path, capsys, needs_toolchain):
    _skip_without_v2_tools(name)
    exp = yaml.safe_load((EXAMPLES / name / "expected.yaml").read_text())

    proj = _fresh(name, tmp_path / "verify" / name)
    _approve(capsys, proj / "spec.yaml")
    rc, data = _cli(capsys, "verify", str(proj / "spec.yaml"))
    assert data["status"] == exp["verify"], json.dumps(data.get("verification", {}).get("stages"), indent=1)[:4000]
    for tool in exp.get("detected_by") or []:
        assert tool in _tools(data), (tool, _tools(data))
    if exp["verify"] not in ("VERIFIED_BOUNDED", "PROVED"):
        assert rc != 0

    for spec_name, vexp in (exp.get("variants") or {}).items():
        _approve(capsys, proj / spec_name)
        rc, vdata = _cli(capsys, "verify", str(proj / spec_name))
        assert vdata["status"] == vexp["verify"], spec_name

    proj2 = _fresh(name, tmp_path / "run" / name)
    _approve(capsys, proj2 / "spec.yaml")
    rc, data = _cli(capsys, "run", str(proj2 / "spec.yaml"), "--provider", "mock", "--regenerate")
    assert data["status"] == exp["run"], json.dumps(data.get("repair_rounds"), indent=1)
    rounds = data.get("repair_rounds") or []
    if "rejected_rounds" in exp:
        assert [r["round"] for r in rounds if r["policy_allowed"] is False] == list(exp["rejected_rounds"])
    if exp["run"] == "VERIFIED_BOUNDED" and exp["verify"] != "VERIFIED_BOUNDED":
        assert rounds, "a repaired example must have repair rounds"
    if exp.get("repaired"):
        # no formal gate (e.g. C++): repair reaches "every required gate passes" and is written back,
        # while the status honestly stays UNKNOWN
        assert rounds and rounds[-1]["policy_allowed"] and not rounds[-1]["reverted"], rounds
        assert data["status"] == "UNKNOWN"
    elif exp["run"] in ("UNKNOWN", "TIMEOUT"):
        assert not rounds, "inconclusive results must not be 'repaired'"
