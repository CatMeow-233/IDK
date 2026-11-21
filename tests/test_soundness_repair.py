from __future__ import annotations

from leanc.foundation.models import (
    FailureReport,
    Location,
    StageResult,
    VerificationResult,
)
from leanc.foundation.status import Outcome, Status
from leanc.pipeline.repair_loop import is_improvement

STATIC_FAILURE_KIND = "static"
COUNTEREXAMPLE_KIND = "counterexample"
NULL_DEREFERENCE = "core.NullDereference"


def _result(status: Status, **outcomes: Outcome) -> VerificationResult:
    stages = [StageResult(stage=stage, outcome=outcome) for stage, outcome in outcomes.items()]
    return VerificationResult(run_id=status.value, status=status, stages=stages)


def _blocked_by_integrity() -> VerificationResult:
    return _result(
        Status.STATIC_ANALYSIS_WARNING,
        compile=Outcome.PASS,
        integrity=Outcome.FAIL,
        sanitizer=Outcome.SKIPPED,
        cbmc=Outcome.SKIPPED,
    )


def _counterexample_after_integrity_passes() -> VerificationResult:
    vr = _result(
        Status.COUNTEREXAMPLE_FOUND,
        compile=Outcome.PASS,
        integrity=Outcome.PASS,
        sanitizer=Outcome.PASS,
        cbmc=Outcome.FAIL,
    )
    vr.stage("cbmc").failures = [
        FailureReport(
            id="safe_array_get.array_bounds.1",
            kind=COUNTEREXAMPLE_KIND,
            tool="cbmc",
            property_id="safe_array_get.array_bounds.1",
            description="array index out of bounds",
            location=Location(file="src/safe_array.c", line=9, function="safe_array_get"),
        )
    ]
    return vr


def test_fixing_integrity_is_progress_even_when_it_exposes_a_counterexample():
    assert is_improvement(_blocked_by_integrity(), _counterexample_after_integrity_passes())


def test_breaking_integrity_to_hide_a_counterexample_is_not_progress():
    assert not is_improvement(_counterexample_after_integrity_passes(), _blocked_by_integrity())


def test_integrity_still_failing_is_ranked_as_before():
    still_blocked = _blocked_by_integrity()
    assert not is_improvement(_blocked_by_integrity(), still_blocked)


def test_fixing_integrity_into_an_inconclusive_result_is_not_progress():
    inconclusive = _result(
        Status.UNKNOWN, compile=Outcome.PASS, integrity=Outcome.PASS, sanitizer=Outcome.PASS, cbmc=Outcome.INCONCLUSIVE
    )
    assert not is_improvement(_blocked_by_integrity(), inconclusive)


def test_fixing_integrity_while_breaking_a_passing_gate_is_not_progress():
    before = _result(
        Status.STATIC_ANALYSIS_WARNING,
        compile=Outcome.PASS,
        static_analysis=Outcome.PASS,
        integrity=Outcome.FAIL,
        cbmc=Outcome.SKIPPED,
    )
    after = _result(
        Status.COUNTEREXAMPLE_FOUND,
        compile=Outcome.PASS,
        static_analysis=Outcome.ERROR,
        integrity=Outcome.PASS,
        cbmc=Outcome.FAIL,
    )
    assert not is_improvement(before, after)


def test_fixing_integrity_while_a_passing_gate_starts_failing_is_not_progress():
    before = _result(
        Status.STATIC_ANALYSIS_WARNING,
        compile=Outcome.PASS,
        static_analysis=Outcome.PASS,
        integrity=Outcome.FAIL,
        cbmc=Outcome.SKIPPED,
    )
    after = _result(
        Status.COUNTEREXAMPLE_FOUND,
        compile=Outcome.PASS,
        static_analysis=Outcome.FAIL,
        integrity=Outcome.PASS,
        cbmc=Outcome.FAIL,
    )
    assert not is_improvement(before, after)


def _analyzer_finding(rule: str, line: int | None = None) -> FailureReport:
    location = Location(file="src/safe_array.c", line=line, function="safe_array_get") if line else None
    return FailureReport(
        id=f"{rule}:{line}", kind=STATIC_FAILURE_KIND, tool="clang-analyzer", description=rule, location=location
    )


def _with_analyzer_findings(vr: VerificationResult, *rules: str) -> VerificationResult:
    stage = vr.stage("static_analysis")
    stage.failures = [_analyzer_finding(rule) for rule in rules]
    return vr


def _with_analyzer_findings_at(vr: VerificationResult, rule: str, *lines: int) -> VerificationResult:
    vr.stage("static_analysis").failures = [_analyzer_finding(rule, line) for line in lines]
    return vr


def _analyzer_failing_and_integrity(integrity: Outcome, status: Status) -> VerificationResult:
    return _result(
        status, compile=Outcome.PASS, static_analysis=Outcome.FAIL, integrity=integrity, cbmc=Outcome.SKIPPED
    )


def test_fixing_integrity_while_adding_static_analysis_findings_is_not_progress():
    before = _with_analyzer_findings(
        _analyzer_failing_and_integrity(Outcome.FAIL, Status.STATIC_ANALYSIS_WARNING), "core.NullDereference"
    )
    after = _with_analyzer_findings(
        _analyzer_failing_and_integrity(Outcome.PASS, Status.STATIC_ANALYSIS_WARNING),
        "core.NullDereference",
        "core.DivideZero",
    )
    assert not is_improvement(before, after)


def test_fixing_integrity_with_the_same_static_analysis_findings_is_progress():
    before = _with_analyzer_findings(
        _analyzer_failing_and_integrity(Outcome.FAIL, Status.STATIC_ANALYSIS_WARNING), "core.NullDereference"
    )
    after = _with_analyzer_findings(
        _analyzer_failing_and_integrity(Outcome.PASS, Status.STATIC_ANALYSIS_WARNING), "core.NullDereference"
    )
    assert is_improvement(before, after)


def test_fixing_integrity_while_repeating_a_finding_elsewhere_in_the_function_is_not_progress():
    before = _with_analyzer_findings_at(
        _analyzer_failing_and_integrity(Outcome.FAIL, Status.STATIC_ANALYSIS_WARNING), NULL_DEREFERENCE, 5
    )
    after = _with_analyzer_findings_at(
        _analyzer_failing_and_integrity(Outcome.PASS, Status.STATIC_ANALYSIS_WARNING), NULL_DEREFERENCE, 5, 9
    )
    assert not is_improvement(before, after)


def test_fixing_integrity_with_a_finding_that_only_moved_lines_is_progress():
    before = _with_analyzer_findings_at(
        _analyzer_failing_and_integrity(Outcome.FAIL, Status.STATIC_ANALYSIS_WARNING), NULL_DEREFERENCE, 5
    )
    after = _with_analyzer_findings_at(
        _analyzer_failing_and_integrity(Outcome.PASS, Status.STATIC_ANALYSIS_WARNING), NULL_DEREFERENCE, 8
    )
    assert is_improvement(before, after)
