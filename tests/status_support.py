from __future__ import annotations

from leanc.foundation.models import StageResult
from leanc.foundation.status import Outcome, outcome_to_status
from leanc.pipeline.controller import INDUCTIVE_GATE

GATES_BEFORE_CBMC = ("compile", "integrity", "static_analysis", "sanitizer")


def stage_result(name: str, outcome: Outcome, required: bool = True) -> StageResult:
    return StageResult(
        stage=name,
        outcome=outcome,
        status=outcome_to_status(name, outcome),
        summary=f"{name} {outcome.value}",
        required=required,
    )


def gate_stages(cbmc: Outcome | None, inductive: Outcome | None, wp: Outcome | None) -> list[StageResult]:
    stages = [stage_result("spec", Outcome.PASS)] + [stage_result(name, Outcome.PASS) for name in GATES_BEFORE_CBMC]
    for name, outcome in (("cbmc", cbmc), (INDUCTIVE_GATE, inductive), ("wp", wp)):
        if outcome is not None:
            stages.append(stage_result(name, outcome))
    return stages
