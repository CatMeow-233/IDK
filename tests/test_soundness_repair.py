from __future__ import annotations

from leanc.foundation.models import StageResult, VerificationResult
from leanc.foundation.status import Outcome, Status
from leanc.pipeline.repair_loop import is_improvement


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
    return _result(
        Status.COUNTEREXAMPLE_FOUND,
        compile=Outcome.PASS,
        integrity=Outcome.PASS,
        sanitizer=Outcome.PASS,
        cbmc=Outcome.FAIL,
    )


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
