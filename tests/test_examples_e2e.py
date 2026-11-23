from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from leanc.cli import main
from leanc.foundation.status import Status, is_success
from leanc.specs.loader import load_spec
from leanc.toolchain.cxx import find_clangxx
from leanc.toolchain.framac import MIN_FRAMAC_MAJOR, find_framac, framac_version_key
from leanc.toolchain.lean import find_lean
from leanc.toolchain.tools import find_clang, tool_version

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
CASES = sorted(p.parent.name for p in EXAMPLES.glob("*/expected.yaml"))
GATE_FAILED_IN_SANITIZER_AND_CBMC = {("gate_failed", "sanitizer"), ("gate_failed", "cbmc")}
EXPECTED_VERIFY_REASON_CODES: dict[str, set[tuple[str, str]]] = {
    "array_oob": GATE_FAILED_IN_SANITIZER_AND_CBMC,
    "cbmc_timeout": {("gate_timeout", "cbmc")},
    "cheating_repair": GATE_FAILED_IN_SANITIZER_AND_CBMC,
    "cpp_bounds_bug": {("gate_failed", "sanitizer")},
    "cpp_ring_buffer": {("no_formal_gate", "spec")},
    "div_by_zero": GATE_FAILED_IN_SANITIZER_AND_CBMC,
    "int_overflow": GATE_FAILED_IN_SANITIZER_AND_CBMC,
    "lean_clamp": {("property_unproved", "lean")},
    "loop_unwind": {("unwind_insufficient", "cbmc"), ("vacuous_harness", "cbmc")},
    "null_deref": GATE_FAILED_IN_SANITIZER_AND_CBMC | {("gate_failed", "static_analysis")},
    "repair_exhausted": GATE_FAILED_IN_SANITIZER_AND_CBMC,
    "ring_buffer": set(),
    "runtime_memory": {("gate_failed", "sanitizer")},
    "safe_array_get": set(),
    "vacuous_harness": {("vacuous_harness", "cbmc")},
    "wp_contract_violation": {("gate_failed", "cbmc"), ("property_unproved", "wp")},
    "wp_proof_repair": {("property_unproved", "wp")},
    "wp_safe_array_get": set(),
}


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


def _reason_codes(payload: dict) -> list[dict]:
    return payload["verification"]["reason_codes"]


def _assert_codes_only_without_success(payload: dict) -> None:
    verification_status = Status(payload["verification"]["status"])
    assert bool(_reason_codes(payload)) is not is_success(verification_status), _reason_codes(payload)


def test_every_example_declares_its_verify_reason_codes():
    assert set(EXPECTED_VERIFY_REASON_CODES) == set(CASES)


def _skip_without_v2_tools(name: str) -> None:
    spec = load_spec(EXAMPLES / name / "spec.yaml")
    if "wp" in spec.gates:
        fc = find_framac()
        if fc is None or framac_version_key(tool_version(fc.real, "-version"))[0] < MIN_FRAMAC_MAJOR:
            pytest.skip("Frama-C/WP (>= 27) not available")
    if "lean" in spec.gates:
        lt = find_lean()
        if lt is None or not lt.leanchecker:
            pytest.skip("Lean 4 toolchain with leanchecker not available")
    if spec.language.is_cpp:
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
    _assert_codes_only_without_success(data)
    assert {(c["code"], c["stage"]) for c in _reason_codes(data)} == EXPECTED_VERIFY_REASON_CODES[name]
    if not is_success(Status(exp["verify"])):
        assert rc != 0

    for spec_name, vexp in (exp.get("variants") or {}).items():
        _approve(capsys, proj / spec_name)
        rc, vdata = _cli(capsys, "verify", str(proj / spec_name))
        assert vdata["status"] == vexp["verify"], spec_name
        _assert_codes_only_without_success(vdata)

    proj2 = _fresh(name, tmp_path / "run" / name)
    _approve(capsys, proj2 / "spec.yaml")
    rc, data = _cli(capsys, "run", str(proj2 / "spec.yaml"), "--provider", "mock", "--regenerate")
    assert data["status"] == exp["run"], json.dumps(data.get("repair_rounds"), indent=1)
    if "verification" in data:
        _assert_codes_only_without_success(data)
    rounds = data.get("repair_rounds") or []
    if "rejected_rounds" in exp:
        assert [r["round"] for r in rounds if r["policy_allowed"] is False] == list(exp["rejected_rounds"])
    if exp["run"] == "VERIFIED_BOUNDED" and exp["verify"] != "VERIFIED_BOUNDED":
        assert rounds, "a repaired example must have repair rounds"
    if exp.get("repaired"):
        last_round_accepted = rounds and rounds[-1]["policy_allowed"] and not rounds[-1]["reverted"]
        assert last_round_accepted, rounds
        assert data["status"] == "UNKNOWN", "without a formal gate a written-back repair stays UNKNOWN"
    elif exp["run"] in ("UNKNOWN", "TIMEOUT"):
        assert not rounds, "inconclusive results must not be 'repaired'"
