from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from leanc.backends.counterexample.json_fields import as_dict, as_list
from leanc.backends.counterexample.locations import (
    is_cprover_pseudo_file,
    location_from_cbmc,
)
from leanc.backends.counterexample.names import (
    clean_name,
    is_internal_function,
    is_internal_name,
)
from leanc.backends.counterexample.values import render_value
from leanc.foundation.models import Location, TraceStep

MAX_TRACE_STEPS = 80
MAX_TRACE_HEAD_STEPS = 10
TRACE_HEAD_FRACTION = 4
MAX_ARGUMENT_CHARS = 60
UNKNOWN_FUNCTION = "?"


def steps_of(trace: Any) -> list[dict]:
    return [step for step in as_list(trace) if isinstance(step, dict)]


def function_id(step: dict) -> str | None:
    function = as_dict(step.get("function"))
    identifier = function.get("identifier") or function.get("displayName")
    return str(identifier) if identifier else None


def failure_index(steps: list[dict], property_id: str | None = None) -> int:
    last_failure = None
    for index, step in enumerate(steps):
        if step.get("stepType") == "failure":
            if property_id is not None and step.get("property") == property_id:
                return index
            last_failure = index
    return last_failure if last_failure is not None else len(steps)


def _lhs_text(step: dict) -> str:
    lhs = step.get("lhs")
    return lhs if isinstance(lhs, str) else ""


def is_actual_parameter(step: dict) -> bool:
    return step.get("stepType") == "assignment" and step.get("assignmentType") == "actual-parameter"


@dataclass
class _OpenCall:
    step: TraceStep
    function: str
    arguments: list[str] = field(default_factory=list)

    def add_argument(self, assignment: dict) -> None:
        value = render_value(assignment.get("value"), max_chars=MAX_ARGUMENT_CHARS)
        self.arguments.append(f"{clean_name(_lhs_text(assignment))}={value}")
        self.step.text = f"call {self.function}({', '.join(self.arguments)})"


def _call_location(step: dict, own_location: Location | None, caller_position: Location | None) -> Location | None:
    if own_location is not None:
        return own_location
    return caller_position or location_from_cbmc(as_dict(step.get("function")).get("sourceLocation"))


def _open_call(step: dict, location: Location | None, caller_position: Location | None) -> _OpenCall | None:
    function = function_id(step) or UNKNOWN_FUNCTION
    if is_internal_function(function):
        return None
    call = TraceStep(
        kind="function-call", location=_call_location(step, location, caller_position), text=f"call {function}()"
    )
    return _OpenCall(step=call, function=function)


def _inside_cprover_library_model(location: Location | None) -> bool:
    return location is not None and bool(location.file) and is_cprover_pseudo_file(location.file)


def _assignment_step(step: dict, location: Location | None) -> TraceStep | None:
    lhs = _lhs_text(step)
    if is_internal_name(lhs) or _inside_cprover_library_model(location):
        return None
    return TraceStep(kind="assignment", location=location, text=f"{clean_name(lhs)} = {render_value(step.get('value'))}")


def _return_step(step: dict, location: Location | None) -> TraceStep | None:
    function = function_id(step) or UNKNOWN_FUNCTION
    if is_internal_function(function):
        return None
    return TraceStep(kind="function-return", location=location, text=f"return from {function}()")


def _failure_step(step: dict, location: Location | None) -> TraceStep:
    reason = step.get("reason") or "property violated"
    violated = step.get("property")
    text = f"FAILURE {violated}: {reason}" if violated else f"FAILURE: {reason}"
    return TraceStep(kind="failure", location=location, text=text)


def _assumption_step(step: dict, location: Location | None) -> TraceStep:
    return TraceStep(kind="assume", location=location, text="assumption")


def _output_step(step: dict, location: Location | None) -> TraceStep:
    values = ", ".join(render_value(value, max_chars=MAX_ARGUMENT_CHARS) for value in as_list(step.get("values")))
    label = step.get("outputID") or "output"
    return TraceStep(kind="output", location=location, text=f"{label}: {values}".strip())


_StepRenderer = Callable[[dict, Location | None], TraceStep | None]

_RENDERED_STEP_TYPES: dict[str, _StepRenderer] = {
    "function-return": _return_step,
    "failure": _failure_step,
    "assumption": _assumption_step,
    "output": _output_step,
}


def _readable_steps(steps: list[dict]) -> list[TraceStep]:
    readable: list[TraceStep] = []
    open_call: _OpenCall | None = None
    last_location: Location | None = None
    for step in steps:
        step_type = step.get("stepType")
        location = location_from_cbmc(step.get("sourceLocation"))
        caller_position = last_location
        if location is not None:
            last_location = location
        if step.get("hidden"):
            continue
        if step_type == "function-call":
            open_call = _open_call(step, location, caller_position)
            if open_call is not None:
                readable.append(open_call.step)
            continue
        if is_actual_parameter(step):
            if open_call is not None:
                open_call.add_argument(step)
            continue
        open_call = None
        render = _assignment_step if step_type == "assignment" else _RENDERED_STEP_TYPES.get(str(step_type))
        rendered = render(step, location) if render is not None else None
        if rendered is not None:
            readable.append(rendered)
    return readable


def _excerpt(steps: list[TraceStep], max_steps: int) -> list[TraceStep]:
    if max_steps <= 0:
        return []
    if len(steps) <= max_steps:
        return steps
    head_count = min(MAX_TRACE_HEAD_STEPS, max_steps // TRACE_HEAD_FRACTION)
    tail_count = max_steps - head_count - 1
    if tail_count < 1:
        return steps[-max_steps:]
    omitted = len(steps) - head_count - tail_count
    marker = TraceStep(kind="location", text=f"... {omitted} step(s) omitted ...")
    return [*steps[:head_count], marker, *steps[-tail_count:]]


def summarize_trace(trace: Any, max_steps: int = MAX_TRACE_STEPS) -> list[TraceStep]:
    return _excerpt(_readable_steps(steps_of(trace)), max_steps)


@dataclass
class _Frame:
    function: str
    position: Location | None = None


def _enter_call(stack: list[_Frame], step: dict, call_site: Location | None) -> None:
    if stack and call_site is not None:
        stack[-1].position = call_site
    stack.append(_Frame(function_id(step) or UNKNOWN_FUNCTION))


def _pop_to_matching_frame(stack: list[_Frame], function: str | None) -> None:
    for depth in range(len(stack) - 1, -1, -1):
        if stack[depth].function == function:
            del stack[depth:]
            return


def _return_from_call(stack: list[_Frame], function: str | None) -> None:
    if not stack:
        return
    if function is None or stack[-1].function == function:
        stack.pop()
        return
    _pop_to_matching_frame(stack, function)


def _frames_at_failure(steps: list[dict], property_id: str | None) -> list[_Frame]:
    stack: list[_Frame] = []
    at_failure: list[_Frame] | None = None
    for step in steps:
        step_type = step.get("stepType")
        location = location_from_cbmc(step.get("sourceLocation"))
        if step_type == "function-call":
            _enter_call(stack, step, location)
            continue
        if step_type == "function-return":
            _return_from_call(stack, function_id(step))
            continue
        if location is not None and stack and location.function == stack[-1].function:
            stack[-1].position = location
        if step_type == "failure":
            at_failure = [_Frame(frame.function, frame.position) for frame in stack]
            if property_id is not None and step.get("property") == property_id:
                break
    return at_failure if at_failure is not None else stack


def _frame_text(frame: _Frame) -> str:
    position = frame.position
    if position is not None and (position.file or position.line is not None):
        where = ":".join(str(part) for part in (position.file or "?", position.line) if part is not None)
        return f"{frame.function} ({where})"
    return str(frame.function)


def call_stack_at_failure(trace: Any, property_id: str | None = None) -> list[str]:
    frames = _frames_at_failure(steps_of(trace), property_id)
    return [_frame_text(frame) for frame in frames if not is_internal_function(frame.function)]
