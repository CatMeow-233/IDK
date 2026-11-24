from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from leanc.backends.cbmc.bodiless import unmodelled_return_values_in_trace
from leanc.backends.cbmc.output import PROPERTY_HOLDS
from leanc.backends.counterexample.locations import location_from_cbmc
from leanc.backends.counterexample.properties import CBMC_TOOL, is_unwinding_property, property_class
from leanc.foundation.models import FailureReport, Location, StageReasonCode
from leanc.foundation.reasons import Reason

FAILURE_ID_PREFIX = "inductive:"
UNPROVED_KIND = "unproved"
INDUCTIVE_STEP_CLASSES = frozenset(
    {"loop_invariant_base", "loop_invariant_step", "loop_assigns", "loop_step_unwinding", "assigns", "frees"}
)
NON_TERMINATION_CLASS = "loop_decreases"
UNDEFINED_FUNCTION_DESCRIPTION = "undefined function should be unreachable"
NOT_A_COUNTEREXAMPLE = (
    " It is not a counterexample: the inductive run starts every claimed call and every contracted loop from an "
    "arbitrary state admitted by the harness assumptions and the loop invariants, so the failing state may be "
    "unreachable in real executions."
)
EXPLANATIONS: dict[Reason, str] = {
    Reason.INDUCTIVE_STEP_FAILED: (
        "CBMC could not show that the loop contract holds: the invariant at loop entry or after one step from "
        "an arbitrary state satisfying it, or that the loop writes only what its assigns clause names."
    ),
    Reason.SUSPECTED_NON_TERMINATION: (
        "CBMC could not show that the decreases clause strictly decreases on every step of the loop, so the "
        "loop may not terminate."
    ),
    Reason.UNWIND_INSUFFICIENT: (
        "An unwinding assertion failed: a loop or recursion outside the contracted loops (in the harness or a "
        "library model) needs more iterations than the unwind bound of the inductive run."
    ),
    Reason.UNMODELLED_CALLEE: (
        "The program calls a function that has no body; CBMC cannot model its effect, so nothing that "
        "depends on the call is verified."
    ),
    Reason.PROPERTY_UNPROVED: "CBMC could not show this property for every state admitted by the harness assumptions.",
}


@dataclass(frozen=True)
class Finding:
    reason: Reason
    detail: str
    location: Location | None = None
    failure: FailureReport | None = None

    def reason_code(self) -> dict:
        return StageReasonCode(code=self.reason, location=self.location, detail=self.detail).model_dump(mode="json")


def failure_reason(result: dict, unmodelled: frozenset[str]) -> Reason:
    if is_unwinding_property(result):
        return Reason.UNWIND_INSUFFICIENT
    cls = property_class(result.get("property"), result.get("description"))
    if cls in INDUCTIVE_STEP_CLASSES:
        return Reason.INDUCTIVE_STEP_FAILED
    if cls == NON_TERMINATION_CLASS:
        return Reason.SUSPECTED_NON_TERMINATION
    location = location_from_cbmc(result.get("sourceLocation"))
    in_unmodelled = location is not None and location.function in unmodelled
    if str(result.get("description")) == UNDEFINED_FUNCTION_DESCRIPTION or in_unmodelled:
        return Reason.UNMODELLED_CALLEE
    if unmodelled_return_values_in_trace(result, unmodelled):
        return Reason.UNMODELLED_CALLEE
    return Reason.PROPERTY_UNPROVED


def unproved_failure(result: dict, reason: Reason, raw_ref: str | None) -> FailureReport:
    property_id = str(result.get("property") or "unknown")
    return FailureReport(
        id=f"{FAILURE_ID_PREFIX}{property_id}",
        kind=UNPROVED_KIND,
        tool=CBMC_TOOL,
        property_id=property_id,
        description=str(result.get("description") or property_id),
        location=location_from_cbmc(result.get("sourceLocation")),
        explanation=EXPLANATIONS[reason] + NOT_A_COUNTEREXAMPLE,
        raw_ref=raw_ref,
    )


def property_findings(failed: Iterable[dict], unmodelled: frozenset[str], raw_ref: str | None) -> list[Finding]:
    findings = []
    for result in failed:
        reason = failure_reason(result, unmodelled)
        failure = unproved_failure(result, reason, raw_ref)
        findings.append(Finding(reason, f"{failure.property_id}: {failure.description}", failure.location, failure))
    return findings


def not_holding(results: Sequence[dict]) -> list[dict]:
    return [result for result in results if str(result.get("status", "")).upper() != PROPERTY_HOLDS]


def callee_findings(functions: Iterable[str]) -> list[Finding]:
    return [
        Finding(Reason.UNMODELLED_CALLEE, f"{function}(): no body, not modelled by CBMC") for function in functions
    ]


def reason_codes(findings: Iterable[Finding]) -> list[dict]:
    return [finding.reason_code() for finding in findings]


def failures(findings: Iterable[Finding]) -> list[FailureReport]:
    return [finding.failure for finding in findings if finding.failure is not None]
