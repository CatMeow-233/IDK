from __future__ import annotations

from pathlib import Path

import pytest

from leanc.cli import main
from leanc.foundation.models import StageResult
from leanc.foundation.status import Outcome, Status, outcome_to_status
from leanc.pipeline import controller
from leanc.pipeline.controller import (
    CBMC_MEMORY_MODEL_LIMITATION,
    INTEGRITY_FAILED,
    INTEGRITY_NOT_RUN,
    LEAK_SANITIZER_LIMITATION,
    NO_LEAK_CHECK_LIMITATION,
    TEST_PATHS_ONLY_LEAK_LIMITATION,
    memory_limitations,
    verify,
)
from leanc.specs.loader import load_spec

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
CANDIDATE_GATES = ("sanitizer", "cbmc", "wp", "lean")
SPURIOUS = "spurious finding on rejected code"
WP_EXAMPLE = "wp_safe_array_get"
LEAN_EXAMPLE = "lean_clamp"
PLAIN_EXAMPLE = "safe_array_get"
SAFE_ARRAY_CHECKS = "checks: [unsigned-overflow, conversion]"
SAFE_ARRAY_GATES = "gates: [compile, static_analysis, sanitizer, cbmc]"
TESTS_SECTION = "tests:\n"


def _stage(stage: str, outcome: Outcome, summary: str = "stub") -> StageResult:
    return StageResult(stage=stage, outcome=outcome, status=outcome_to_status(stage, outcome), summary=summary)


class _StubGates:
    def __init__(self, integrity: Outcome, candidate: Outcome = Outcome.FAIL) -> None:
        self.integrity = integrity
        self.candidate = candidate
        self.called: list[str] = []

    def runner(self, gate: str):
        def run(ctx) -> StageResult:
            self.called.append(gate)
            if gate == "integrity":
                return _stage(gate, self.integrity, "stub integrity verdict")
            if gate in CANDIDATE_GATES:
                return _stage(gate, self.candidate, SPURIOUS)
            return _stage(gate, Outcome.PASS)

        return run

    def install(self, monkeypatch) -> _StubGates:
        for gate in list(controller._GATE_RUNNERS):
            monkeypatch.setitem(controller._GATE_RUNNERS, gate, self.runner(gate))
        return self


def _all_pass(monkeypatch) -> _StubGates:
    return _StubGates(Outcome.PASS, candidate=Outcome.PASS).install(monkeypatch)


def _approved(example_copy, example: str, edits: dict[str, str] | None = None) -> Path:
    root = example_copy(example)
    spec_file = root / "spec.yaml"
    text = spec_file.read_text()
    for old, new in (edits or {}).items():
        assert old in text, old
        text = text.replace(old, new)
    spec_file.write_text(text)
    assert main(["spec", "approve", str(spec_file), "--by", "reviewer", "--yes"]) == 0
    return root


def _verify(root: Path, tmp_path: Path):
    return verify(root / "spec.yaml", project_root=root, run_dir=tmp_path / "run")


def _stages(result) -> dict[str, StageResult]:
    return {stage.stage: stage for stage in result.stages}


@pytest.mark.parametrize("example", [WP_EXAMPLE, LEAN_EXAMPLE])
def test_integrity_failure_skips_every_gate_that_analyses_or_runs_the_candidate(
    example, example_copy, tmp_path, monkeypatch, capsys
):
    root = _approved(example_copy, example)
    stub = _StubGates(Outcome.FAIL).install(monkeypatch)
    result = _verify(root, tmp_path)
    gates = set(load_spec(root / "spec.yaml").gates) & set(CANDIDATE_GATES)
    assert gates and not gates & set(stub.called), stub.called
    stages = _stages(result)
    for gate in gates:
        assert stages[gate].outcome is Outcome.SKIPPED
        assert INTEGRITY_FAILED in stages[gate].summary
    assert result.status is Status.STATIC_ANALYSIS_WARNING
    assert not any(SPURIOUS in reason for reason in result.reasons), result.reasons


def test_integrity_error_status_comes_from_integrity_not_from_cbmc(example_copy, tmp_path, monkeypatch, capsys):
    root = _approved(example_copy, PLAIN_EXAMPLE)
    stub = _StubGates(Outcome.ERROR).install(monkeypatch)
    result = _verify(root, tmp_path)
    assert "cbmc" not in stub.called and "sanitizer" not in stub.called
    assert _stages(result)["cbmc"].outcome is Outcome.SKIPPED
    assert result.status is Status.TOOL_ERROR


@pytest.mark.parametrize("example", [WP_EXAMPLE, LEAN_EXAMPLE])
def test_candidate_gates_still_run_after_integrity_passes(example, example_copy, tmp_path, monkeypatch, capsys):
    root = _approved(example_copy, example)
    stub = _StubGates(Outcome.PASS).install(monkeypatch)
    result = _verify(root, tmp_path)
    gates = set(load_spec(root / "spec.yaml").gates) & set(CANDIDATE_GATES)
    assert gates <= set(stub.called), stub.called
    assert result.status is Status.COUNTEREXAMPLE_FOUND


def test_static_analysis_still_runs_after_integrity_failure(example_copy, tmp_path, monkeypatch, capsys):
    root = _approved(example_copy, PLAIN_EXAMPLE)
    stub = _StubGates(Outcome.FAIL).install(monkeypatch)
    _verify(root, tmp_path)
    assert "static_analysis" in stub.called


def test_leak_sanitizer_and_cbmc_limitations_are_reported(example_copy, tmp_path, monkeypatch, capsys):
    root = _approved(example_copy, PLAIN_EXAMPLE)
    _all_pass(monkeypatch)
    result = _verify(root, tmp_path)
    assert result.status is Status.VERIFIED_BOUNDED
    assert LEAK_SANITIZER_LIMITATION in result.limitations
    assert TEST_PATHS_ONLY_LEAK_LIMITATION in result.limitations
    assert CBMC_MEMORY_MODEL_LIMITATION in result.limitations
    assert NO_LEAK_CHECK_LIMITATION not in result.limitations


def test_cbmc_memory_leak_check_removes_the_test_paths_only_limitation(example_copy, tmp_path, monkeypatch, capsys):
    edits = {SAFE_ARRAY_CHECKS: "checks: [unsigned-overflow, conversion, memory-leak]"}
    root = _approved(example_copy, PLAIN_EXAMPLE, edits)
    _all_pass(monkeypatch)
    result = _verify(root, tmp_path)
    assert LEAK_SANITIZER_LIMITATION in result.limitations
    assert TEST_PATHS_ONLY_LEAK_LIMITATION not in result.limitations
    assert NO_LEAK_CHECK_LIMITATION not in result.limitations


def test_no_leak_check_is_reported_without_lsan_or_cbmc_memory_leak(example_copy, tmp_path, monkeypatch, capsys):
    root = _approved(example_copy, PLAIN_EXAMPLE, {TESTS_SECTION: TESTS_SECTION + "  sanitizers: [address, undefined]\n"})
    _all_pass(monkeypatch)
    result = _verify(root, tmp_path)
    assert NO_LEAK_CHECK_LIMITATION in result.limitations
    assert LEAK_SANITIZER_LIMITATION not in result.limitations
    assert TEST_PATHS_ONLY_LEAK_LIMITATION not in result.limitations
    assert CBMC_MEMORY_MODEL_LIMITATION in result.limitations


def test_skipped_gates_contribute_no_coverage_limitations(example_copy, tmp_path, monkeypatch, capsys):
    root = _approved(example_copy, PLAIN_EXAMPLE)
    _StubGates(Outcome.FAIL).install(monkeypatch)
    result = _verify(root, tmp_path)
    assert NO_LEAK_CHECK_LIMITATION in result.limitations
    assert LEAK_SANITIZER_LIMITATION not in result.limitations
    assert CBMC_MEMORY_MODEL_LIMITATION not in result.limitations


@pytest.mark.parametrize("undecided", [Outcome.ERROR, Outcome.TIMEOUT, Outcome.INCONCLUSIVE])
def test_leak_checks_without_a_verdict_are_not_counted(undecided):
    spec = load_spec(EXAMPLES / PLAIN_EXAMPLE / "spec.yaml")
    spec.verification.checks.append("memory-leak")
    stages = [StageResult(stage="sanitizer", outcome=undecided), StageResult(stage="cbmc", outcome=undecided)]
    assert memory_limitations(spec, stages) == [NO_LEAK_CHECK_LIMITATION]


def test_cbmc_leak_verdict_without_lsan_verdict_keeps_only_the_cbmc_limits():
    spec = load_spec(EXAMPLES / PLAIN_EXAMPLE / "spec.yaml")
    spec.verification.checks.append("memory-leak")
    stages = [StageResult(stage="sanitizer", outcome=Outcome.ERROR), StageResult(stage="cbmc", outcome=Outcome.PASS)]
    assert memory_limitations(spec, stages) == [CBMC_MEMORY_MODEL_LIMITATION]



def test_candidate_gates_never_run_when_compile_and_integrity_are_absent(example_copy, tmp_path, monkeypatch, capsys):
    root = _approved(example_copy, PLAIN_EXAMPLE, {SAFE_ARRAY_GATES: "gates: [cbmc]"})
    stub = _all_pass(monkeypatch)
    result = _verify(root, tmp_path)
    assert "cbmc" not in stub.called
    cbmc = _stages(result)["cbmc"]
    assert cbmc.outcome is Outcome.SKIPPED and INTEGRITY_NOT_RUN in cbmc.summary
    assert result.status is Status.UNKNOWN
