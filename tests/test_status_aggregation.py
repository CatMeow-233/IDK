"""Status vocabulary and controller aggregation rules (no external tools)."""

from __future__ import annotations

import pytest

from leanc.core.controller import aggregate
from leanc.core.models import StageResult
from leanc.core.status import (
    EXIT_CODES,
    Outcome,
    Status,
    exit_code,
    is_success,
    outcome_to_status,
    rank,
    worst,
)


def st(stage: str, outcome: Outcome, status: Status | None = None, required: bool = True) -> StageResult:
    return StageResult(
        stage=stage,
        outcome=outcome,
        status=status if status is not None else outcome_to_status(stage, outcome),
        summary=f"{stage} {outcome.value}",
        required=required,
    )


SPEC_OK = st("spec", Outcome.PASS)


def test_every_status_has_distinct_exit_code_and_only_success_is_zero():
    for s in Status:
        assert s in EXIT_CODES
    zero = {s for s, c in EXIT_CODES.items() if c == 0}
    assert zero == {Status.VERIFIED_BOUNDED, Status.PROVED}
    non_zero = [c for s, c in EXIT_CODES.items() if c != 0]
    assert len(non_zero) == len(set(non_zero))


def test_rank_order_definite_failures_dominate_inconclusive():
    assert worst([Status.TIMEOUT, Status.COUNTEREXAMPLE_FOUND]) is Status.COUNTEREXAMPLE_FOUND
    assert worst([Status.UNKNOWN, Status.STATIC_ANALYSIS_WARNING]) is Status.STATIC_ANALYSIS_WARNING
    assert worst([Status.VERIFIED_BOUNDED, Status.UNKNOWN]) is Status.UNKNOWN
    assert worst([Status.SPEC_UNAPPROVED, Status.COMPILE_ERROR]) is Status.SPEC_UNAPPROVED
    assert rank(Status.REPAIR_EXHAUSTED) < rank(Status.COMPILE_ERROR)
    assert worst([]) is Status.UNKNOWN


def test_success_predicate():
    assert is_success(Status.VERIFIED_BOUNDED)
    assert is_success(Status.PROVED)
    for s in Status:
        if s not in (Status.VERIFIED_BOUNDED, Status.PROVED):
            assert not is_success(s)


def test_unapproved_spec_is_always_spec_unapproved_even_if_gates_pass():
    spec = st("spec", Outcome.FAIL, Status.SPEC_UNAPPROVED)
    spec.details["reasons"] = ["hash mismatch"]
    stages = [spec, st("compile", Outcome.PASS), st("cbmc", Outcome.PASS)]
    status, reasons = aggregate(stages, approved=False)
    assert status is Status.SPEC_UNAPPROVED
    assert reasons == ["hash mismatch"]


def test_all_gates_pass_with_cbmc_is_verified_bounded_not_proved():
    stages = [SPEC_OK] + [st(g, Outcome.PASS) for g in ("compile", "static_analysis", "sanitizer", "cbmc")]
    status, _ = aggregate(stages, approved=True)
    assert status is Status.VERIFIED_BOUNDED


def test_no_formal_gate_cannot_be_verified():
    stages = [SPEC_OK, st("compile", Outcome.PASS), st("sanitizer", Outcome.PASS)]
    status, reasons = aggregate(stages, approved=True)
    assert status is Status.UNKNOWN
    assert "formal" in reasons[0]


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (Outcome.FAIL, Status.COUNTEREXAMPLE_FOUND),
        (Outcome.INCONCLUSIVE, Status.UNKNOWN),
        (Outcome.TIMEOUT, Status.TIMEOUT),
        (Outcome.ERROR, Status.TOOL_ERROR),
        (Outcome.SKIPPED, Status.UNKNOWN),
    ],
)
def test_cbmc_non_pass_outcomes(outcome, expected):
    stages = [SPEC_OK, st("compile", Outcome.PASS), st("cbmc", outcome)]
    status, reasons = aggregate(stages, approved=True)
    assert status is expected
    assert any("[cbmc]" in r for r in reasons)


def test_compile_error_dominates_skipped_gates():
    stages = [
        SPEC_OK,
        st("compile", Outcome.FAIL),
        st("static_analysis", Outcome.SKIPPED),
        st("cbmc", Outcome.SKIPPED),
    ]
    assert aggregate(stages, approved=True)[0] is Status.COMPILE_ERROR


def test_static_warning_beats_cbmc_timeout():
    stages = [SPEC_OK, st("compile", Outcome.PASS), st("static_analysis", Outcome.FAIL), st("cbmc", Outcome.TIMEOUT)]
    assert aggregate(stages, approved=True)[0] is Status.STATIC_ANALYSIS_WARNING


def test_skipped_sanitizer_without_sandbox_prevents_verified():
    stages = [SPEC_OK, st("compile", Outcome.PASS), st("sanitizer", Outcome.SKIPPED), st("cbmc", Outcome.PASS)]
    assert aggregate(stages, approved=True)[0] is Status.UNKNOWN


def test_link_failure_in_sanitizer_stage_reports_compile_error():
    stages = [SPEC_OK, st("compile", Outcome.PASS), st("sanitizer", Outcome.FAIL, Status.COMPILE_ERROR), st("cbmc", Outcome.PASS)]
    assert aggregate(stages, approved=True)[0] is Status.COMPILE_ERROR


def test_exit_code_mapping():
    assert exit_code(Status.COUNTEREXAMPLE_FOUND) == 13
    assert exit_code(Status.SPEC_UNAPPROVED) == 40
    assert exit_code(Status.REPAIR_EXHAUSTED) == 50
