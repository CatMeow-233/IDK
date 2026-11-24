from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from inductive_iset_support import CONTRACTED_LINE, SOURCE

from leanc.foundation.models import (
    FailureReport,
    Location,
    StageReasonCode,
    StageResult,
    VerificationResult,
)
from leanc.foundation.reasons import STAGE_REASON_CODES_DETAIL, Reason
from leanc.foundation.status import Outcome
from leanc.llm.base import RepairRequest
from leanc.llm.mock import MockProvider, repair_fixture
from leanc.pipeline.controller import aggregate
from leanc.pipeline.reason_codes import derive_reason_codes
from leanc.specs.model import INDUCTIVE_GATE

UNPROVED_KIND = "unproved"
LOOP_FUNCTION = "lower_bound"
LOOP_LOCATION = Location(file=SOURCE, line=CONTRACTED_LINE, function=LOOP_FUNCTION)
GATES_BEFORE_PROOFS = ("spec", "compile", "integrity", "static_analysis")
RESIDUAL_DETAIL = (
    "after loop-contract instrumentation these loops still have no contract, so CBMC is not run: "
    f"{SOURCE}:{CONTRACTED_LINE} (CBMC loop {LOOP_FUNCTION}.0)"
)
REJECTED_DETAIL = f"a loop contract does not match exactly one loop: {SOURCE}:14: no for or while loop starts on this line"
STEP_PROPERTY = f"{LOOP_FUNCTION}.loop_invariant_step.1"
DECREASES_PROPERTY = f"{LOOP_FUNCTION}.loop_decreases.1"
OVERFLOW_PROPERTY = f"{LOOP_FUNCTION}.overflow.6"
VACUOUS_PROBE = f"leanc_loop:{SOURCE}:{CONTRACTED_LINE}"


def unproved(property_id: str, description: str, tool: str = "cbmc") -> FailureReport:
    return FailureReport(
        id=f"{INDUCTIVE_GATE}:{property_id}",
        kind=UNPROVED_KIND,
        tool=tool,
        property_id=property_id,
        description=description,
        location=LOOP_LOCATION,
    )


STEP_FAILURE = unproved(STEP_PROPERTY, "Check invariant after step for loop lower_bound.0")
DECREASES_FAILURE = unproved(DECREASES_PROPERTY, "Check variant decreases after step for loop lower_bound.0")
OVERFLOW_FAILURE = unproved(OVERFLOW_PROPERTY, "arithmetic overflow on signed + in lo + hi")
WP_FAILURES = (
    unproved("wp_goal_post_1", "Post-condition 'ok' not proved", "frama-c-wp"),
    unproved("wp_goal_loop_2", "Loop invariant not preserved", "frama-c-wp"),
)
REASON_OF_FAILURE = {
    STEP_PROPERTY: Reason.INDUCTIVE_STEP_FAILED,
    DECREASES_PROPERTY: Reason.SUSPECTED_NON_TERMINATION,
}


def stage_code(reason: Reason, detail: str, location: Location | None = None) -> dict:
    return StageReasonCode(code=reason, location=location, detail=detail).model_dump(mode="json")


def failure_code(failure: FailureReport) -> dict:
    detail = f"{failure.property_id}: {failure.description}"
    return stage_code(REASON_OF_FAILURE.get(failure.property_id, Reason.PROPERTY_UNPROVED), detail, failure.location)


def inductive_stage(
    outcome: Outcome, failures: Iterable[FailureReport] = (), extra_codes: Iterable[dict] = ()
) -> StageResult:
    failures = list(failures)
    codes = [] if outcome is Outcome.PASS else [*(failure_code(f) for f in failures), *extra_codes]
    return StageResult(
        stage=INDUCTIVE_GATE, outcome=outcome, failures=failures, details={STAGE_REASON_CODES_DETAIL: codes}
    )


def residual_stage() -> StageResult:
    return inductive_stage(Outcome.INCONCLUSIVE, extra_codes=[stage_code(Reason.LOOP_WITHOUT_CONTRACT, RESIDUAL_DETAIL)])


def wp_stage(outcome: Outcome, failures: Iterable[FailureReport] = WP_FAILURES) -> StageResult:
    return StageResult(stage="wp", outcome=outcome, failures=list(failures) if outcome is not Outcome.PASS else [])


def verification(
    *proof_stages: StageResult, cbmc: Outcome = Outcome.PASS, cbmc_failures: Iterable[FailureReport] | None = None
) -> VerificationResult:
    stages = [StageResult(stage=name, outcome=Outcome.PASS) for name in GATES_BEFORE_PROOFS]
    if cbmc_failures is None:
        cbmc_failures = [unproved("iset_rank.overflow.1", "bounded overflow")] if cbmc is Outcome.FAIL else []
    stages.append(StageResult(stage="cbmc", outcome=cbmc, failures=list(cbmc_failures)))
    stages.extend(proof_stages)
    status, reasons = aggregate(stages, approved=True)
    return VerificationResult(
        run_id="synthetic",
        status=status,
        reasons=reasons,
        stages=stages,
        reason_codes=derive_reason_codes(stages, status),
    )


class RecordingMockProvider(MockProvider):
    def __init__(self, fixture_dir: Path):
        super().__init__(fixture_dir)
        self.requests: list[RepairRequest] = []

    def propose_patch(self, req: RepairRequest):
        self.requests.append(req)
        return super().propose_patch(req)


def write_mock_rounds(fixture_dir: Path, *rounds: dict[str, str], root_cause: str | None = None) -> Path:
    for number, files in enumerate(rounds, start=1):
        path = fixture_dir / repair_fixture(number)
        path.parent.mkdir(parents=True, exist_ok=True)
        cause = root_cause or f"round {number}"
        proposal = {"root_cause": cause, "rationale": f"proposal {number}", "files": files}
        path.write_text(json.dumps(proposal, indent=1), encoding="utf-8")
    return fixture_dir
