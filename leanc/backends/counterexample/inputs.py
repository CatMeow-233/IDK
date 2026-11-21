from __future__ import annotations

import copy
from typing import Any

from leanc.backends.counterexample.json_fields import as_dict
from leanc.backends.counterexample.locations import relative_file, same_file
from leanc.backends.counterexample.names import clean_name, is_internal_name
from leanc.backends.counterexample.trace import failure_index, function_id, is_actual_parameter, steps_of
from leanc.backends.counterexample.values import parse_lhs, render_value, set_path, value_type
from leanc.foundation.models import CounterexampleInput

HARNESS_ENTRY_POINT = "main"
PARAMETER_PREFIX = "param "


def _in_harness_main(step: dict, harness_file: str | None) -> bool:
    source_location = as_dict(step.get("sourceLocation"))
    if source_location.get("function") != HARNESS_ENTRY_POINT:
        return False
    if not harness_file:
        return True
    return same_file(relative_file(source_location.get("file"), source_location.get("workingDirectory")), harness_file)


def _last_target_call(steps: list[dict], failure_at: int, target_function: str | None) -> int | None:
    if not target_function:
        return None
    last_call = None
    for index, step in enumerate(steps[: failure_at + 1]):
        if step.get("stepType") == "function-call" and function_id(step) == target_function:
            last_call = index
    return last_call


def _is_harness_user_assignment(step: dict, harness_file: str | None) -> bool:
    if step.get("hidden") or step.get("stepType") != "assignment" or is_actual_parameter(step):
        return False
    if not _in_harness_main(step, harness_file):
        return False
    lhs = step.get("lhs")
    return isinstance(lhs, str) and not is_internal_name(lhs)


def _fold_assignment(values: dict[str, dict], lhs: str, value: dict) -> None:
    parsed = parse_lhs(lhs)
    if parsed is None:
        return
    base, path = parsed
    if not path:
        values[base] = copy.deepcopy(value)
        return
    if base in values and set_path(values[base], path, value):
        return
    values[clean_name(lhs)] = copy.deepcopy(value)


def _harness_values(steps: list[dict], harness_file: str | None) -> dict[str, dict]:
    values: dict[str, dict] = {}
    for step in steps:
        if _is_harness_user_assignment(step, harness_file):
            _fold_assignment(values, step["lhs"], as_dict(step.get("value")))
    return values


def _call_parameters(steps_after_call: list[dict]) -> list[CounterexampleInput]:
    parameters = []
    for step in steps_after_call:
        if is_actual_parameter(step):
            name = clean_name(step.get("lhs")) or "?"
            value = as_dict(step.get("value"))
            parameters.append(
                CounterexampleInput(name=f"{PARAMETER_PREFIX}{name}", value=render_value(value), type=value_type(value))
            )
            continue
        if step.get("hidden"):
            continue
        break
    return parameters


def extract_inputs(
    trace: Any,
    harness_file: str | None,
    target_function: str | None,
    *,
    property_id: str | None = None,
) -> list[CounterexampleInput]:
    steps = steps_of(trace)
    failure_at = failure_index(steps, property_id)
    target_call = _last_target_call(steps, failure_at, target_function)
    assignments_end = target_call if target_call is not None else min(failure_at, len(steps))
    inputs = [
        CounterexampleInput(name=name, value=render_value(value), type=value_type(value))
        for name, value in _harness_values(steps[:assignments_end], harness_file).items()
    ]
    if target_call is not None:
        inputs += _call_parameters(steps[target_call + 1 :])
    return inputs
