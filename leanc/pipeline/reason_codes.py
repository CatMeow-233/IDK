from __future__ import annotations

from collections.abc import Iterable

from leanc.backends.wp.compile_flags import unapplied_compile_flags
from leanc.foundation.models import FailureReport, ReasonCode, StageReasonCode, StageResult
from leanc.foundation.reasons import STAGE_REASON_CODES_DETAIL, Reason
from leanc.foundation.status import Outcome, Status, is_success

SPEC_STAGE = "spec"
BOUNDED_GATE = "cbmc"
PROVING_GATE = "wp"
NO_FORMAL_GATE_DETAIL = "the spec requires neither a cbmc nor a wp gate"
REASON_OF_FAILURE_KIND: dict[str, Reason] = {
    "unwind": Reason.UNWIND_INSUFFICIENT,
    "vacuity": Reason.VACUOUS_HARNESS,
    "unproved": Reason.PROPERTY_UNPROVED,
}
REASON_OF_OUTCOME: dict[Outcome, Reason] = {
    Outcome.FAIL: Reason.GATE_FAILED,
    Outcome.SKIPPED: Reason.GATE_SKIPPED,
    Outcome.TIMEOUT: Reason.GATE_TIMEOUT,
    Outcome.ERROR: Reason.TOOL_ERROR,
    Outcome.INCONCLUSIVE: Reason.GATE_INCONCLUSIVE,
}
VACUITY_DETAIL = "vacuity"
VACUITY_STATUSES_NOT_CHECKED = ("disabled", "unchecked")
BODILESS_DETAIL = "bodiless_functions"
UNMODELLED_KEY = "unmodelled"
UNDECLARED_DETAIL = "undeclared_functions"
MODEL_VERDICT_DETAIL = "frama_c_model_verdict"
MODEL_ONLY_PREFIX = "compile.flags not applied by Frama-C: "


def _code(reason: Reason, stage: StageResult, detail: str = "", failure: FailureReport | None = None) -> ReasonCode:
    return ReasonCode(code=reason, stage=stage.stage, location=failure.location if failure else None, detail=detail)


def _first_line(text: str) -> str:
    return text.partition("\n")[0]


def _failure_detail(failure: FailureReport) -> str:
    return f"{failure.property_id or failure.id}: {_first_line(failure.description)}"


def _failure_codes(stage: StageResult) -> list[ReasonCode]:
    return [
        _code(REASON_OF_FAILURE_KIND[failure.kind], stage, _failure_detail(failure), failure)
        for failure in stage.failures
        if failure.kind in REASON_OF_FAILURE_KIND
    ]


def _vacuity_codes(stage: StageResult) -> list[ReasonCode]:
    status = (stage.details.get(VACUITY_DETAIL) or {}).get("status")
    if status not in VACUITY_STATUSES_NOT_CHECKED:
        return []
    return [_code(Reason.VACUITY_UNCHECKED, stage, f"vacuity status {status}")]


def _function_codes(reason: Reason, stage: StageResult, functions: Iterable[str]) -> list[ReasonCode]:
    return [_code(reason, stage, f"{function}()") for function in functions]


def _callee_codes(stage: StageResult) -> list[ReasonCode]:
    unmodelled = (stage.details.get(BODILESS_DETAIL) or {}).get(UNMODELLED_KEY) or []
    undeclared = stage.details.get(UNDECLARED_DETAIL) or []
    return _function_codes(Reason.UNMODELLED_CALLEE, stage, unmodelled) + _function_codes(
        Reason.UNDECLARED_FUNCTION, stage, undeclared
    )


def _is_frama_c_model_only_proof(stage: StageResult) -> bool:
    model_verdict = stage.details.get(MODEL_VERDICT_DETAIL) or {}
    return stage.outcome is Outcome.INCONCLUSIVE and model_verdict.get("outcome") == Outcome.PASS.value


def _model_only_codes(stage: StageResult) -> list[ReasonCode]:
    if not _is_frama_c_model_only_proof(stage):
        return []
    detail = MODEL_ONLY_PREFIX + ", ".join(unapplied_compile_flags(stage.details))
    return [_code(Reason.FRAMA_C_MODEL_ONLY_PROOF, stage, detail)]


def _derived_codes(stage: StageResult) -> list[ReasonCode]:
    return _failure_codes(stage) + _vacuity_codes(stage) + _callee_codes(stage) + _model_only_codes(stage)


def _supplied_codes(stage: StageResult) -> list[ReasonCode]:
    supplied = [StageReasonCode.model_validate(entry) for entry in stage.details[STAGE_REASON_CODES_DETAIL]]
    return [ReasonCode(code=s.code, stage=stage.stage, location=s.location, detail=s.detail) for s in supplied]


def _specific_codes(stage: StageResult) -> list[ReasonCode]:
    if STAGE_REASON_CODES_DETAIL in stage.details:
        return _supplied_codes(stage)
    return _derived_codes(stage)


def stage_reason_codes(stage: StageResult) -> list[ReasonCode]:
    if stage.outcome is Outcome.PASS:
        return []
    specific = _specific_codes(stage)
    if stage.outcome is Outcome.INCONCLUSIVE and specific:
        return specific
    return [_code(REASON_OF_OUTCOME[stage.outcome], stage, stage.summary), *specific]


def _not_passed(stages: list[StageResult]) -> list[StageResult]:
    return [stage for stage in stages if stage.outcome is not Outcome.PASS]


def _stages_explaining(stages: list[StageResult], status: Status) -> list[StageResult]:
    if status is Status.SPEC_UNAPPROVED:
        return _not_passed([stage for stage in stages if stage.stage == SPEC_STAGE])
    contributing = _not_passed([stage for stage in stages if stage.stage != SPEC_STAGE and stage.required])
    if contributing:
        return contributing
    bounded = next((stage for stage in stages if stage.stage == BOUNDED_GATE), None)
    return _not_passed([bounded] if bounded is not None else [])


def _counts_as_formal_gate(stage: StageResult) -> bool:
    return stage.stage == BOUNDED_GATE or (stage.stage == PROVING_GATE and stage.required)


def _no_formal_gate(stages: list[StageResult], status: Status) -> list[ReasonCode]:
    if status is not Status.UNKNOWN or any(_counts_as_formal_gate(stage) for stage in stages):
        return []
    return [ReasonCode(code=Reason.NO_FORMAL_GATE, stage=SPEC_STAGE, detail=NO_FORMAL_GATE_DETAIL)]


def _unique(codes: list[ReasonCode]) -> list[ReasonCode]:
    unique: list[ReasonCode] = []
    for code in codes:
        if code not in unique:
            unique.append(code)
    return unique


def derive_reason_codes(stages: list[StageResult], status: Status) -> list[ReasonCode]:
    if is_success(status):
        return []
    explained = [code for stage in _stages_explaining(stages, status) for code in stage_reason_codes(stage)]
    return _unique(explained + _no_formal_gate(stages, status))
