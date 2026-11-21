from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from leanc.backends.counterexample.fingerprints import fingerprint
from leanc.backends.sanitizer import gate as sanitizer_gate
from leanc.backends.sanitizer import ownership
from leanc.cli import main
from leanc.reports.json_report import read_verification

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "examples" / "runtime_memory"
LEAKING_CASES = {"separator_in_field", "nul_in_field"}
APPROVAL_STAGE = "spec"
pytestmark = pytest.mark.usefixtures("needs_toolchain")


def _verify(tmp_path: Path, capsys, name: str):
    project = tmp_path / name
    shutil.copytree(EXAMPLE, project, ignore=shutil.ignore_patterns(".leanc"))
    spec = project / "spec.yaml"
    assert main(["spec", "approve", str(spec), "--by", "e2e", "--yes", "--json"]) == 0
    capsys.readouterr()
    rc = main(["verify", str(spec), "--json"])
    payload = json.loads(capsys.readouterr().out)
    result = read_verification(Path(payload["run_dir"]) / "verify" / "verification.json")
    return rc, payload, result, next(s for s in result.stages if s.stage == "sanitizer")


def _verdict(rc, result, sanitizer):
    failures = [
        (f.id, f.kind, f.tool, f.property_id, f.description, f.location, fingerprint(f)) for f in result.all_failures()
    ]
    stages = [(s.stage, s.outcome, s.status, s.summary if s.stage != APPROVAL_STAGE else "") for s in result.stages]
    return rc, result.status, stages, failures, sanitizer.details["counts"]


def test_leak_failures_carry_the_ownership_trace(tmp_path, capsys):
    rc, payload, result, sanitizer = _verify(tmp_path, capsys, "traced")
    assert rc == 12 and result.status == "RUNTIME_TEST_FAILED"
    leaks = [f for f in sanitizer.failures if f.tool == "lsan"]
    assert {f.inputs[0].value for f in leaks} == LEAKING_CASES
    for failure in leaks:
        trace = failure.ownership_trace
        assert trace is not None, sanitizer.details.get("ownership_trace")
        assert (trace.allocation.file, trace.allocation.line, trace.allocation.function) == ("src/join.c", 22, "join_with_sep")
        assert [(loc.file, loc.function) for loc in trace.released_at] == [("tests/test_join.c", "expect_ok")]
        assert trace.function.function == "join_with_sep" and trace.loss in {"inside-function", "at-function-return"}
        assert trace.returned_to is not None and trace.returned_to.function == "expect_err"
    summary_entries = [f for f in payload["verification"]["failures"] if f["tool"] == "lsan"]
    assert all(entry["ownership_trace"]["allocation"]["line"] == 22 for entry in summary_entries)
    assert "ownership-trace-run" in {i.tool for i in sanitizer.invocations}


def test_verdict_is_identical_with_and_without_tracing(tmp_path, capsys, monkeypatch):
    traced = _verify(tmp_path, capsys, "traced")
    monkeypatch.setattr(sanitizer_gate, "attach_ownership_traces", lambda *args: None)
    untraced = _verify(tmp_path, capsys, "untraced")
    assert _verdict(traced[0], traced[2], traced[3]) == _verdict(untraced[0], untraced[2], untraced[3])
    assert all(f.ownership_trace is None for f in untraced[2].all_failures())


@pytest.mark.parametrize(
    "forged",
    [
        "A 0x502000000010 6 0x1\nX 1 0x2 0x3\n",
        "E 0x1 0x2\nA 0x502000000010 6 0xffffffffffffffff\nX 1 0x1 0x2\n",
        "\x00garbage",
    ],
)
def test_forged_trace_logs_only_remove_the_evidence(tmp_path, capsys, monkeypatch, forged):
    traced = _verify(tmp_path, capsys, "traced")
    monkeypatch.setattr(ownership, "_read_trace_log", lambda path: ownership._LogRead(text=forged))
    forged_run = _verify(tmp_path, capsys, "forged")
    assert _verdict(traced[0], traced[2], traced[3]) == _verdict(forged_run[0], forged_run[2], forged_run[3])
    assert all(f.ownership_trace is None for f in forged_run[2].all_failures())
    assert forged_run[3].details["ownership_trace"]["cases"]


def test_passing_project_runs_no_trace(tmp_path, capsys):
    project = tmp_path / "safe"
    shutil.copytree(REPO / "examples" / "safe_array_get", project, ignore=shutil.ignore_patterns(".leanc"))
    spec = project / "spec.yaml"
    assert main(["spec", "approve", str(spec), "--by", "e2e", "--yes", "--json"]) == 0
    capsys.readouterr()
    assert main(["verify", str(spec), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    result = read_verification(Path(payload["run_dir"]) / "verify" / "verification.json")
    sanitizer = next(s for s in result.stages if s.stage == "sanitizer")
    assert "ownership_trace" not in sanitizer.details
    assert not any(i.tool.startswith("ownership") for i in sanitizer.invocations)
