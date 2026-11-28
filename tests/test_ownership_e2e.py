from __future__ import annotations

import contextlib
import io
import json
import shutil
from pathlib import Path

import pytest

from leanc.backends.counterexample.fingerprints import fingerprint
from leanc.backends.sanitizer import gate as sanitizer_gate
from leanc.backends.sanitizer import ownership, ownership_replay
from leanc.backends.sanitizer.ownership_build import TraceBuild
from leanc.backends.sanitizer.ownership_replay import Replay
from leanc.backends.sanitizer.ownership_symbols import Symbolized
from leanc.cli import main
from leanc.foundation.models import Location, MemoryOwnershipTrace
from leanc.reports.json_report import read_verification

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "examples" / "runtime_memory"
LEAKING_CASES = {"separator_in_field", "nul_in_field"}
APPROVAL_STAGE = "spec"
FORGED_TRACE = MemoryOwnershipTrace(
    test_case="forged", allocation=Location(file="src/join.c", line=1), loss="not-localised"
)
pytestmark = pytest.mark.usefixtures("needs_toolchain")


def _cli(*argv: str) -> tuple[int, dict]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main([*argv, "--json"])
    return rc, json.loads(out.getvalue())


def _verify(root: Path, name: str, example: Path = EXAMPLE):
    project = root / name
    shutil.copytree(example, project, ignore=shutil.ignore_patterns(".leanc"))
    spec = project / "spec.yaml"
    assert _cli("spec", "approve", str(spec), "--by", "e2e", "--yes")[0] == 0
    rc, payload = _cli("verify", str(spec))
    result = read_verification(Path(payload["run_dir"]) / "verify" / "verification.json")
    return rc, payload, result, next(s for s in result.stages if s.stage == "sanitizer")


def _verdict(run):
    rc, _, result, sanitizer = run
    failures = [
        (f.id, f.kind, f.tool, f.property_id, f.description, f.location, f.inputs, f.raw_ref, fingerprint(f))
        for f in result.all_failures()
    ]
    stages = [
        (s.stage, s.outcome, s.status, s.summary if s.stage != APPROVAL_STAGE else "", [d.model_dump(exclude={"raw"}) for d in s.diagnostics])
        for s in result.stages
    ]
    return rc, result.status, stages, failures, sanitizer.details["counts"]


@pytest.fixture(scope="module")
def traced(tmp_path_factory):
    return _verify(tmp_path_factory.mktemp("baseline"), "traced")


def test_leak_failures_carry_the_ownership_trace(traced, needs_symbolizer):
    rc, payload, result, sanitizer = traced
    assert rc == 12 and result.status == "RUNTIME_TEST_FAILED"
    leaks = [f for f in sanitizer.failures if f.tool == "lsan"]
    assert {f.inputs[0].value for f in leaks} == LEAKING_CASES
    for failure in leaks:
        trace = failure.ownership_trace
        assert trace is not None, sanitizer.details.get("ownership_trace")
        assert (trace.allocation.file, trace.allocation.line, trace.allocation.function) == ("src/join.c", 22, "join_with_sep")
        assert [(loc.file, loc.function) for loc in trace.released_at] == [("tests/test_join.c", "expect_ok")]
        assert trace.loss == "lost-during-call" and trace.function.function == "join_with_sep"
        assert trace.returned_to is not None and trace.returned_to.function == "expect_err"
    summary_entries = [f for f in payload["verification"]["failures"] if f["tool"] == "lsan"]
    assert all(entry["ownership_trace"]["allocation"]["line"] == 22 for entry in summary_entries)
    assert "ownership-trace-run" in {i.tool for i in sanitizer.invocations}


def _attach_forged(candidates, failures, project_files):
    for failure in failures:
        failure.ownership_trace = FORGED_TRACE
    return ["forged"]


FAILURE_MODES = {
    "disabled": (sanitizer_gate, "attach_ownership_traces", lambda *args: None),
    "build-failure": (ownership, "build_trace_binary", lambda *args: TraceBuild(problem="forced build failure")),
    "replay-timeout": (ownership, "replay", lambda *args: Replay(problem="replay did not complete (timeout)")),
    "symbolize-failure": (ownership, "symbolize", lambda *args: Symbolized(problem="forced symbolizer failure")),
    "budget-exhausted": (ownership._Session, "out_of_time", lambda self: True),
    "forged-log": (ownership_replay, "read_trace_log", lambda path: ownership_replay._LogRead(text="E 0x1 0x2\nX 1 0x3 0x2\n")),
    "garbage-log": (ownership_replay, "read_trace_log", lambda path: ownership_replay._LogRead(text="\x00garbage")),
    "forged-evidence-attached": (ownership, "attach_traces", _attach_forged),
}


@pytest.mark.parametrize("mode", sorted(FAILURE_MODES))
def test_verdict_is_identical_whatever_happens_to_the_trace(traced, tmp_path, monkeypatch, mode, request):
    if mode == "forged-evidence-attached":
        request.getfixturevalue("needs_symbolizer")
    target, name, replacement = FAILURE_MODES[mode]
    monkeypatch.setattr(target, name, replacement)
    other = _verify(tmp_path, mode)
    assert _verdict(other) == _verdict(traced)
    if mode == "forged-evidence-attached":
        assert all(f.ownership_trace == FORGED_TRACE for f in other[3].failures if f.tool == "lsan")
    else:
        assert all(f.ownership_trace is None for f in other[2].all_failures())


def test_passing_project_runs_no_trace(tmp_path):
    rc, _, result, sanitizer = _verify(tmp_path, "safe", REPO / "examples" / "safe_array_get")
    assert rc == 0 and "ownership_trace" not in sanitizer.details
    assert not any(i.tool.startswith("ownership") for i in sanitizer.invocations)
