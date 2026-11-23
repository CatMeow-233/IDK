from __future__ import annotations

import itertools

import pytest
from status_support import GATES_BEFORE_CBMC, gate_stages, stage_result

from leanc.foundation.models import FailureReport, StageResult
from leanc.foundation.reasons import Reason
from leanc.foundation.status import SUCCESS_STATUSES, Outcome, Status, is_success
from leanc.pipeline.controller import INDUCTIVE_GATE, aggregate
from leanc.pipeline.reason_codes import NO_FORMAL_GATE_DETAIL, REASON_OF_OUTCOME, derive_reason_codes

SPEC = "spec"
CBMC = "cbmc"
WP = "wp"
MATRIX_GATES = (CBMC, INDUCTIVE_GATE, WP)
UNDECIDED_OUTCOMES = (Outcome.FAIL, Outcome.INCONCLUSIVE, Outcome.ERROR, Outcome.TIMEOUT, Outcome.SKIPPED)
GATE_STATES = (None, *itertools.product(Outcome, (True, False)))
GATE_MATRIX = list(itertools.product(GATE_STATES, repeat=len(MATRIX_GATES)))
REVOKED = "after the gates: protected file harness/h.c changed"


def _pairs(codes) -> list[tuple[Reason, str]]:
    return [(code.code, code.stage) for code in codes]


def _derived(stages: list[StageResult], approved: bool = True):
    status, _ = aggregate(stages, approved=approved)
    return status, derive_reason_codes(stages, status)


def _matrix_stages(states: tuple) -> list[StageResult]:
    stages = gate_stages(None, None, None)
    for name, state in zip(MATRIX_GATES, states):
        if state is not None:
            stages.append(stage_result(name, state[0], required=state[1]))
    return stages


@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize("states", GATE_MATRIX)
def test_codes_exist_exactly_when_the_status_is_not_a_success(states, approved):
    stages = _matrix_stages(states)
    if not approved:
        stages[0] = stage_result(SPEC, Outcome.FAIL)
    status, codes = _derived(stages, approved)
    assert bool(codes) is not is_success(status), (status, _pairs(codes))
    names = {stage.stage for stage in stages}
    assert all(code.stage in names for code in codes)
    if not approved:
        assert _pairs(codes) == [(Reason.GATE_FAILED, SPEC)]
        return
    for stage in stages:
        if stage.required and stage.outcome is not Outcome.PASS:
            assert (REASON_OF_OUTCOME[stage.outcome], stage.stage) in _pairs(codes)


@pytest.mark.parametrize("outcome", UNDECIDED_OUTCOMES)
def test_a_non_required_cbmc_stage_that_did_not_pass_explains_the_unknown_status(outcome):
    stages = gate_stages(None, Outcome.PASS, None) + [stage_result(CBMC, outcome, required=False)]
    status, codes = _derived(stages)
    assert status is Status.UNKNOWN
    assert _pairs(codes) == [(REASON_OF_OUTCOME[outcome], CBMC)]


def test_a_spec_without_cbmc_or_wp_gives_no_formal_gate():
    status, codes = _derived(gate_stages(None, None, None))
    assert status is Status.UNKNOWN
    assert _pairs(codes) == [(Reason.NO_FORMAL_GATE, SPEC)]
    assert codes[0].detail == NO_FORMAL_GATE_DETAIL


def test_an_unknown_status_without_formal_gate_lists_the_stage_and_no_formal_gate():
    stages = [stage_result(SPEC, Outcome.PASS), stage_result("compile", Outcome.PASS)]
    stages.append(stage_result("sanitizer", Outcome.SKIPPED))
    status, codes = _derived(stages)
    assert status is Status.UNKNOWN
    assert _pairs(codes) == [(Reason.GATE_SKIPPED, "sanitizer"), (Reason.NO_FORMAL_GATE, SPEC)]


def test_a_definite_failure_without_formal_gate_has_no_no_formal_gate_code():
    stages = [stage_result(SPEC, Outcome.PASS), stage_result("sanitizer", Outcome.FAIL)]
    status, codes = _derived(stages)
    assert status is Status.RUNTIME_TEST_FAILED
    assert _pairs(codes) == [(Reason.GATE_FAILED, "sanitizer")]


def test_a_wp_only_spec_does_not_get_no_formal_gate():
    stages = [stage_result(SPEC, Outcome.PASS), stage_result(WP, Outcome.INCONCLUSIVE)]
    status, codes = _derived(stages)
    assert status is Status.UNKNOWN
    assert _pairs(codes) == [(Reason.GATE_INCONCLUSIVE, WP)]


@pytest.mark.parametrize("outcome", list(Outcome))
def test_a_non_required_wp_stage_without_cbmc_gives_no_formal_gate(outcome):
    stages = [stage_result(SPEC, Outcome.PASS), stage_result(WP, outcome, required=False)]
    status, codes = _derived(stages)
    assert status is Status.UNKNOWN
    assert _pairs(codes) == [(Reason.NO_FORMAL_GATE, SPEC)]


def test_an_unapproved_spec_is_explained_by_the_spec_stage_only():
    stages = [stage_result(SPEC, Outcome.FAIL), stage_result(CBMC, Outcome.FAIL)]
    status, codes = _derived(stages, approved=False)
    assert status is Status.SPEC_UNAPPROVED
    assert _pairs(codes) == [(Reason.GATE_FAILED, SPEC)]


def test_an_approval_revoked_during_the_run_is_explained_by_the_revoking_spec_stage():
    revoked = StageResult(stage=SPEC, outcome=Outcome.FAIL, status=Status.SPEC_UNAPPROVED, summary=REVOKED)
    status, codes = _derived(gate_stages(Outcome.PASS, None, None) + [revoked], approved=False)
    assert status is Status.SPEC_UNAPPROVED
    assert _pairs(codes) == [(Reason.GATE_FAILED, SPEC)]
    assert codes[0].detail == REVOKED


def test_a_compile_error_lists_the_failed_compile_and_every_skipped_gate():
    skipped = [stage_result(gate, Outcome.SKIPPED) for gate in ("static_analysis", "sanitizer", CBMC)]
    stages = [stage_result(SPEC, Outcome.PASS), stage_result("compile", Outcome.FAIL), *skipped]
    status, codes = _derived(stages)
    assert status is Status.COMPILE_ERROR
    assert _pairs(codes) == [(Reason.GATE_FAILED, "compile")] + [(Reason.GATE_SKIPPED, s.stage) for s in skipped]


def test_every_required_stage_that_did_not_pass_contributes_in_stage_order():
    stages = gate_stages(Outcome.INCONCLUSIVE, Outcome.TIMEOUT, Outcome.ERROR)
    status, codes = _derived(stages)
    assert status is Status.TOOL_ERROR
    assert _pairs(codes) == [
        (Reason.GATE_INCONCLUSIVE, CBMC),
        (Reason.GATE_TIMEOUT, INDUCTIVE_GATE),
        (Reason.TOOL_ERROR, WP),
    ]


def test_a_non_required_stage_does_not_explain_a_status_it_did_not_cause():
    stages = gate_stages(Outcome.INCONCLUSIVE, None, None) + [stage_result("lean", Outcome.FAIL, required=False)]
    status, codes = _derived(stages)
    assert status is Status.UNKNOWN
    assert _pairs(codes) == [(Reason.GATE_INCONCLUSIVE, CBMC)]


@pytest.mark.parametrize("status", sorted(SUCCESS_STATUSES))
def test_a_success_status_never_carries_a_code(status):
    unwind = FailureReport(id="cbmc:f.unwind.0", kind="unwind", tool=CBMC, description="unwinding assertion")
    failed = StageResult(stage=CBMC, outcome=Outcome.INCONCLUSIVE, failures=[unwind])
    stages = [stage_result(SPEC, Outcome.PASS)] + [stage_result(g, Outcome.PASS) for g in GATES_BEFORE_CBMC]
    assert derive_reason_codes([*stages, failed], status) == []
