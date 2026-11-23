from __future__ import annotations

from dataclasses import dataclass

import pytest
from cbmc_replay_support import fixture_messages, replaying_cbmc
from cbmc_support import _fake_tools, make_cbmc_project
from pydantic import ValidationError

from leanc.backends.cbmc import run_cbmc
from leanc.foundation.models import FailureReport, Location, StageResult
from leanc.foundation.reasons import STAGE_REASON_CODES_DETAIL, Reason
from leanc.foundation.status import Outcome, Status, outcome_to_status
from leanc.pipeline.controller import INTEGRITY_FAILED, _skipped
from leanc.pipeline.reason_codes import derive_reason_codes, stage_reason_codes

D15_CODES = (
    "unwind_insufficient",
    "vacuous_harness",
    "vacuity_unchecked",
    "unmodelled_callee",
    "frama_c_model_only_proof",
    "suspected_non_termination",
    "inductive_step_failed",
    "property_unproved",
    "contract_rejected",
    "loop_without_contract",
    "gate_skipped",
    "gate_inconclusive",
    "gate_timeout",
    "tool_error",
    "no_formal_gate",
)
CODES_ADDED_TO_D15 = ("undeclared_function", "gate_failed")
CBMC = "cbmc"
WP = "wp"
INDUCTIVE = "inductive"
ARR_GET = {"src": "arr_get_ok", "harness": "arr_get_harness", "function": "arr_get"}
PASSING_VERIFY = "arr_get_ok.verify.json"
PASSING_VACUITY = "arr_get_ok.vacuity.json"
NOTHING_TO_CHECK_SOURCE = '#include "cbmc_fx.h"\nint add(int a, int b)\n{\n    (void)a;\n    (void)b;\n    return 0;\n}\n'
MODEL_ONLY_FLAG = "-funsigned-char"
LOOP = Location(file="src/iset.c", line=11, function="lower_bound")


def _pairs(codes) -> list[tuple[Reason, str]]:
    return [(code.code, code.stage) for code in codes]


def _codes_of(stage: StageResult):
    return derive_reason_codes([stage], stage.status or outcome_to_status(stage.stage, stage.outcome))


def _replayed(tmp_path, verify: str, vacuity: str = PASSING_VACUITY) -> StageResult:
    ctx = make_cbmc_project(tmp_path, **ARR_GET)
    _fake_tools(ctx, cbmc=replaying_cbmc(tmp_path / "fake", fixture_messages(verify), fixture_messages(vacuity)))
    return run_cbmc(ctx)


def test_reason_values_are_the_d15_codes_plus_two():
    assert {reason.value for reason in Reason} == set(D15_CODES + CODES_ADDED_TO_D15)
    assert len(Reason) == len(D15_CODES) + len(CODES_ADDED_TO_D15)


def test_recorded_counterexample_gives_gate_failed(tmp_path):
    stage = _replayed(tmp_path, "arr_get_off_by_one.verify.json")
    assert stage.outcome is Outcome.FAIL
    codes = _codes_of(stage)
    assert _pairs(codes) == [(Reason.GATE_FAILED, CBMC)]
    assert codes[0].detail == stage.summary


def test_recorded_vacuous_run_gives_one_vacuous_harness_code_per_probe_failure(tmp_path):
    stage = _replayed(tmp_path, PASSING_VERIFY, "arr_get_contradictory.vacuity.json")
    assert stage.outcome is Outcome.INCONCLUSIVE
    codes = _codes_of(stage)
    vacuity_failures = [failure for failure in stage.failures if failure.kind == "vacuity"]
    assert len(vacuity_failures) == 3
    assert _pairs(codes) == [(Reason.VACUOUS_HARNESS, CBMC)] * len(vacuity_failures)
    assert [code.location for code in codes] == [failure.location for failure in vacuity_failures]
    assert codes[0].location.short() == "harness/h.c:19 in main()"
    assert 'LEANC_REACH("after_call")' in codes[0].detail


def test_recorded_parse_error_gives_tool_error(tmp_path):
    stage = _replayed(tmp_path, "syntax_error.verify.json")
    assert stage.outcome is Outcome.ERROR
    assert _pairs(_codes_of(stage)) == [(Reason.TOOL_ERROR, CBMC)]


def test_recorded_pass_gives_no_code(tmp_path):
    stage = _replayed(tmp_path, PASSING_VERIFY)
    assert stage.outcome is Outcome.PASS
    assert stage_reason_codes(stage) == []
    assert derive_reason_codes([stage], Status.VERIFIED_BOUNDED) == []


@dataclass(frozen=True)
class _CbmcCase:
    label: str
    project: dict
    expected: list[tuple[Reason, str]]
    detail: str = ""
    located_in: str | None = None
    source: str | None = None


CBMC_CASES = [
    _CbmcCase(
        "insufficient unwind",
        {"src": "count_zeros", "harness": "count_zeros_harness", "function": "count_zeros", "unwind": 3},
        [(Reason.UNWIND_INSUFFICIENT, CBMC)],
        detail="count_zeros.unwind.0: unwinding assertion loop 0",
        located_in="count_zeros",
    ),
    _CbmcCase(
        "counterexample beyond an insufficient unwind",
        {"src": "count_zeros_off_by_one", "harness": "count_zeros_harness", "function": "count_zeros", "unwind": 3},
        [(Reason.GATE_FAILED, CBMC), (Reason.UNWIND_INSUFFICIENT, CBMC)],
    ),
    _CbmcCase(
        "harness without probes",
        {**ARR_GET, "harness": "arr_get_no_probe_harness"},
        [(Reason.VACUITY_UNCHECKED, CBMC)],
        detail="vacuity status unchecked",
    ),
    _CbmcCase(
        "vacuity check disabled",
        {**ARR_GET, "vacuity_check": False},
        [(Reason.VACUITY_UNCHECKED, CBMC)],
        detail="vacuity status disabled",
    ),
    _CbmcCase(
        "partially vacuous harness",
        {**ARR_GET, "harness": "arr_get_partial_vacuity_harness"},
        [(Reason.VACUOUS_HARNESS, CBMC)],
        located_in="main",
    ),
    _CbmcCase(
        "unmodelled helper",
        {"src": "point", "harness": "point_harness", "function": "point_ratio"},
        [(Reason.UNMODELLED_CALLEE, CBMC)],
        detail="helper_without_body()",
    ),
    _CbmcCase(
        "target without a definition",
        {**ARR_GET, "src": "wrong_name"},
        [(Reason.UNMODELLED_CALLEE, CBMC)],
        detail="arr_get()",
    ),
    _CbmcCase(
        "undeclared nondet input",
        {"src": "mul_div_identity", "harness": "mul_div_undeclared_nondet_harness", "function": "mul_div"},
        [(Reason.UNDECLARED_FUNCTION, CBMC)],
        detail="nondet_ulonglong()",
    ),
    _CbmcCase(
        "nothing to check",
        {"src": "add", "harness": "add_harness", "function": "add"},
        [(Reason.GATE_INCONCLUSIVE, CBMC)],
        source=NOTHING_TO_CHECK_SOURCE,
    ),
    _CbmcCase(
        "solver timeout",
        {"src": "mul_div", "harness": "mul_div_harness", "function": "mul_div", "timeout_s": 2},
        [(Reason.GATE_TIMEOUT, CBMC)],
    ),
]


@pytest.mark.parametrize("case", CBMC_CASES, ids=[case.label for case in CBMC_CASES])
def test_real_cbmc_stage_data_gives_its_reason_codes(case, tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, **case.project)
    if case.source is not None:
        (ctx.snapshot_dir / "src" / "impl.c").write_text(case.source)
    stage = run_cbmc(ctx)
    codes = _codes_of(stage)
    assert _pairs(codes) == case.expected, (stage.outcome, stage.summary)
    if case.detail:
        assert codes[-1].detail == case.detail
    if case.located_in:
        assert codes[-1].location is not None and codes[-1].location.function == case.located_in
    if stage.outcome is not Outcome.INCONCLUSIVE:
        assert codes[0].detail == stage.summary


def test_missing_cbmc_gives_tool_error(tmp_path):
    ctx = make_cbmc_project(tmp_path, **ARR_GET)
    _fake_tools(ctx, cbmc=None)
    assert _pairs(_codes_of(run_cbmc(ctx))) == [(Reason.TOOL_ERROR, CBMC)]


def test_skipped_gate_gives_gate_skipped_with_its_summary():
    stage = _skipped(CBMC, INTEGRITY_FAILED)
    codes = _codes_of(stage)
    assert _pairs(codes) == [(Reason.GATE_SKIPPED, CBMC)]
    assert INTEGRITY_FAILED in codes[0].detail


def _wp_stage(outcome: Outcome, model_verdict: dict | None, failures: list[FailureReport] | None = None) -> StageResult:
    return StageResult(
        stage=WP,
        outcome=outcome,
        status=outcome_to_status(WP, outcome),
        summary=f"wp {outcome.value}",
        failures=failures or [],
        details={"frama_c_model_verdict": model_verdict, "compile_flags": {"not_applied": [MODEL_ONLY_FLAG]}},
    )


def test_wp_proof_under_frama_c_model_only_gives_its_code_with_the_unapplied_flags():
    codes = _codes_of(_wp_stage(Outcome.INCONCLUSIVE, {"outcome": "PASS", "summary": "all goals proved"}))
    assert _pairs(codes) == [(Reason.FRAMA_C_MODEL_ONLY_PROOF, WP)]
    assert MODEL_ONLY_FLAG in codes[0].detail


@pytest.mark.parametrize("outcome", [Outcome.ERROR, Outcome.TIMEOUT, Outcome.FAIL])
def test_frama_c_model_only_proof_needs_an_inconclusive_wp_stage(outcome):
    codes = _codes_of(_wp_stage(outcome, {"outcome": "PASS", "summary": "all goals proved"}))
    assert Reason.FRAMA_C_MODEL_ONLY_PROOF not in [code.code for code in codes]


@pytest.mark.parametrize("model_verdict", [None, {"outcome": "INCONCLUSIVE", "summary": "1 goal(s) not proved"}])
def test_wp_inconclusive_without_a_model_proof_is_gate_inconclusive(model_verdict):
    assert _pairs(_codes_of(_wp_stage(Outcome.INCONCLUSIVE, model_verdict))) == [(Reason.GATE_INCONCLUSIVE, WP)]


def test_unproved_goals_give_property_unproved_at_their_location():
    goal = FailureReport(
        id="wp:goal:typed_sum_loop_invariant",
        kind="unproved",
        tool="frama-c-wp",
        property_id="typed_sum_loop_invariant",
        description="loop invariant not proved\nprovers tried: alt-ergo",
        location=LOOP,
    )
    codes = _codes_of(_wp_stage(Outcome.INCONCLUSIVE, {"outcome": "INCONCLUSIVE", "summary": "x"}, [goal]))
    assert _pairs(codes) == [(Reason.PROPERTY_UNPROVED, WP)]
    assert codes[0].location == LOOP
    assert codes[0].detail == "typed_sum_loop_invariant: loop invariant not proved"


def _inductive_stage(outcome: Outcome, supplied: list, failures: list[FailureReport] | None = None) -> StageResult:
    return StageResult(
        stage=INDUCTIVE,
        outcome=outcome,
        status=outcome_to_status(INDUCTIVE, outcome),
        summary=f"inductive {outcome.value}",
        failures=failures or [],
        details={STAGE_REASON_CODES_DETAIL: supplied},
    )


INDUCTIVE_ONLY_CODES = (
    Reason.SUSPECTED_NON_TERMINATION,
    Reason.INDUCTIVE_STEP_FAILED,
    Reason.CONTRACT_REJECTED,
    Reason.LOOP_WITHOUT_CONTRACT,
)


def test_stage_supplied_codes_are_copied_and_replace_the_generic_mapping():
    supplied = [
        {"code": reason.value, "location": LOOP.model_dump(mode="json"), "detail": f"loop lower_bound.0 {reason.value}"}
        for reason in INDUCTIVE_ONLY_CODES
    ]
    unproved = FailureReport(id="inductive:x", kind="unproved", tool="cbmc", description="step failed", location=LOOP)
    codes = stage_reason_codes(_inductive_stage(Outcome.INCONCLUSIVE, supplied, [unproved]))
    assert _pairs(codes) == [(reason, INDUCTIVE) for reason in INDUCTIVE_ONLY_CODES]
    assert all(code.location == LOOP for code in codes)
    assert codes[0].detail == "loop lower_bound.0 suspected_non_termination"


def test_empty_stage_supplied_codes_on_an_inconclusive_stage_give_gate_inconclusive():
    assert _pairs(stage_reason_codes(_inductive_stage(Outcome.INCONCLUSIVE, []))) == [(Reason.GATE_INCONCLUSIVE, INDUCTIVE)]


def test_stage_supplied_codes_follow_the_outcome_code_of_an_undecided_stage():
    supplied = [{"code": Reason.UNMODELLED_CALLEE.value, "detail": "helper()"}]
    codes = stage_reason_codes(_inductive_stage(Outcome.TIMEOUT, supplied))
    assert _pairs(codes) == [(Reason.GATE_TIMEOUT, INDUCTIVE), (Reason.UNMODELLED_CALLEE, INDUCTIVE)]


@pytest.mark.parametrize(
    "entry",
    [
        {"code": "not_a_reason"},
        {"code": Reason.CONTRACT_REJECTED.value, "stage": "cbmc"},
        {"code": Reason.CONTRACT_REJECTED.value, "note": "extra key"},
        {"detail": "no code"},
    ],
)
def test_malformed_stage_supplied_code_raises(entry):
    with pytest.raises(ValidationError):
        stage_reason_codes(_inductive_stage(Outcome.INCONCLUSIVE, [entry]))


def test_identical_codes_are_reported_once():
    failure = FailureReport(id="cbmc:f.unwind.0", kind="unwind", tool=CBMC, property_id="f.unwind.0", description="u")
    stage = StageResult(stage=CBMC, outcome=Outcome.INCONCLUSIVE, failures=[failure, failure.model_copy()])
    assert _pairs(_codes_of(stage)) == [(Reason.UNWIND_INSUFFICIENT, CBMC)]
