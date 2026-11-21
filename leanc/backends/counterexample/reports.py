from __future__ import annotations

from leanc.backends.counterexample.explain import explain_counterexample
from leanc.backends.counterexample.inputs import extract_inputs
from leanc.backends.counterexample.json_fields import as_dict
from leanc.backends.counterexample.locations import location_from_cbmc
from leanc.backends.counterexample.properties import CBMC_TOOL, VACUITY_ID_PREFIX
from leanc.backends.counterexample.trace import (
    call_stack_at_failure,
    failure_index,
    steps_of,
    summarize_trace,
)
from leanc.foundation.models import FailureReport, Location

MAX_UNWIND_TRACE_STEPS = 30
UNWIND_CUTOFF_CAVEAT = (
    " Unwinding assertions also failed in this verification: the probe may be unreachable only "
    "because executions needing more loop iterations than the unwind bound were not explored."
)


def _failure_location(result: dict, steps: list[dict], property_id: str) -> Location | None:
    location = location_from_cbmc(result.get("sourceLocation"))
    if location is not None:
        return location
    failure_at = failure_index(steps, property_id)
    if failure_at < len(steps):
        return location_from_cbmc(steps[failure_at].get("sourceLocation"))
    return None


def failure_from_cbmc_property(
    result: dict,
    *,
    raw_ref: str | None,
    harness: str | None,
    target_function: str | None,
) -> FailureReport:
    fields = as_dict(result)
    property_id = str(fields.get("property") or "unknown")
    steps = steps_of(fields.get("trace"))
    failure = FailureReport(
        id=f"{CBMC_TOOL}:{property_id}",
        kind="counterexample",
        tool=CBMC_TOOL,
        property_id=property_id,
        description=str(fields.get("description") or property_id),
        location=_failure_location(fields, steps, property_id),
        inputs=extract_inputs(steps, harness, target_function, property_id=property_id),
        call_stack=call_stack_at_failure(steps, property_id),
        trace=summarize_trace(steps),
        raw_ref=raw_ref,
    )
    failure.explanation = explain_counterexample(failure)
    return failure


def unwind_failure(
    result: dict,
    *,
    raw_ref: str | None = None,
    unwind: int | None = None,
    harness: str | None = None,
    target_function: str | None = None,
) -> FailureReport:
    fields = as_dict(result)
    property_id = str(fields.get("property") or "unknown.unwind")
    steps = steps_of(fields.get("trace"))
    failure = FailureReport(
        id=f"{CBMC_TOOL}:{property_id}",
        kind="unwind",
        tool=CBMC_TOOL,
        property_id=property_id,
        description=str(fields.get("description") or "unwinding assertion"),
        location=location_from_cbmc(fields.get("sourceLocation")),
        inputs=extract_inputs(steps, harness, target_function, property_id=property_id) if steps else [],
        call_stack=call_stack_at_failure(steps, property_id) if steps else [],
        trace=summarize_trace(steps, max_steps=MAX_UNWIND_TRACE_STEPS) if steps else [],
        raw_ref=raw_ref,
    )
    failure.explanation = explain_counterexample(failure, unwind=unwind)
    return failure


def _vacuity_description(label: str, reported: bool) -> str:
    if reported:
        return f'vacuity probe LEANC_REACH("{label}") is unreachable under the harness assumptions'
    return (
        f'vacuity probe LEANC_REACH("{label}") was not reported by CBMC '
        "(the code containing it is not reachable from the harness entry point)"
    )


def vacuity_failure(
    label: str,
    *,
    result: dict | None = None,
    raw_ref: str | None = None,
    reported: bool = True,
    unwind_insufficient: bool = False,
) -> FailureReport:
    fields = as_dict(result)
    failure = FailureReport(
        id=f"{VACUITY_ID_PREFIX}{label}",
        kind="vacuity",
        tool=CBMC_TOOL,
        property_id=fields.get("property") if isinstance(fields.get("property"), str) else None,
        description=_vacuity_description(label, reported),
        location=location_from_cbmc(fields.get("sourceLocation")),
        raw_ref=raw_ref,
    )
    failure.explanation = explain_counterexample(failure)
    if unwind_insufficient:
        failure.explanation += UNWIND_CUTOFF_CAVEAT
    return failure
