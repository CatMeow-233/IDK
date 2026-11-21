from __future__ import annotations

from collections.abc import Iterable

from leanc.backends.counterexample.properties import property_class
from leanc.foundation.models import CounterexampleInput, FailureReport

MAX_EXPLAINED_INPUTS = 12

_DEREFERENCE_HINTS = (
    ("pointer null", "A NULL pointer is dereferenced."),
    ("outside object bounds", "Memory outside the bounds of an object is accessed (for example an off-by-one index)."),
    ("deallocated dynamic object", "Memory is accessed after it was freed (use after free)."),
    ("dead object", "An object is accessed after its lifetime ended (e.g. a pointer to a returned local variable)."),
    ("invalid integer address", "A pointer made from an arbitrary integer is dereferenced."),
    ("pointer invalid", "An invalid pointer is dereferenced."),
)
_GENERIC_DEREFERENCE_HINT = "An invalid pointer dereference can occur."

_CLASS_HINTS = {
    "array_bounds": "An array is indexed outside its declared bounds.",
    "division-by-zero": "A division or remainder by zero can occur.",
    "undefined-shift": "A shift is undefined in C (negative or too-large shift distance, or shifting a negative value).",
    "assertion": "An assertion of the approved harness (a functional postcondition or frame condition) does not hold.",
    "memory-leak": "Dynamically allocated memory is not freed.",
    "pointer_arithmetic": "Pointer arithmetic leaves the bounds of an object or overflows.",
    "pointer_primitives": "A pointer primitive is applied to an invalid pointer.",
    "precondition_instance": "A precondition of a C library function (as modelled by CBMC) is violated.",
    "NaN": "A floating-point operation produces NaN.",
    "enum-range-check": "An enum-typed expression holds a value outside the enumeration.",
}

_COUNTEREXAMPLE_SCOPE = (
    "The counterexample is a concrete execution under CBMC's C semantics within the approved "
    "harness constraints and bounds; see the raw trace for every step."
)
_UNWIND_CONSEQUENCE = (
    "Executions beyond the bound were not explored, so the result is not sound for the whole input "
    "space. Raising the bound (verification.unwind / unwindset) changes the specification and "
    "requires a re-approved spec; it is not a defect of the code under test unless the loop "
    "should terminate sooner."
)
_VACUITY_CONSEQUENCE = (
    "The harness assumptions (LEANC_ASSUME) or the code make this point "
    "unreachable, so every check after it holds vacuously and proves nothing. The harness or its "
    "assumptions must be reviewed (a change requires re-approval)."
)


def _dereference_hint(description: str) -> str:
    for marker, hint in _DEREFERENCE_HINTS:
        if marker in description:
            return hint
    return _GENERIC_DEREFERENCE_HINT


def _overflow_hint(description: str) -> str:
    if "signed" in description and "unsigned" not in description:
        return "A signed integer operation overflows, which is undefined behaviour in C."
    if "unsigned" in description:
        return "An unsigned integer operation wraps around (enabled by the unsigned-overflow check)."
    if "conversion" in description or "type conversion" in description:
        return "A value is converted to a type that cannot represent it."
    if "float" in description:
        return "A floating-point operation overflows to infinity."
    return "An arithmetic overflow can occur."


def _hint(property_cls: str, description: str) -> str:
    lowered = description.lower()
    if property_cls == "pointer_dereference":
        return _dereference_hint(lowered)
    if property_cls == "overflow":
        return _overflow_hint(lowered)
    return _CLASS_HINTS.get(property_cls, "")


def _format_inputs(inputs: Iterable[CounterexampleInput], limit: int = MAX_EXPLAINED_INPUTS) -> str:
    items = list(inputs)
    parts = [f"{item.name} = {item.value}" for item in items[:limit]]
    if len(items) > limit:
        parts.append(f"... (+{len(items) - limit} more)")
    return ", ".join(parts)


def _explain_violation(failure: FailureReport, where: str) -> str:
    parts = [
        f"CBMC found an execution of the approved harness that violates property "
        f"{failure.property_id or '?'} ({failure.description}){where}."
    ]
    hint = _hint(property_class(failure.property_id, failure.description), failure.description)
    if hint:
        parts.append(hint)
    if failure.call_stack:
        parts.append(f"Call stack at the failure: {' -> '.join(failure.call_stack)}.")
    if failure.inputs:
        parts.append(f"Inputs leading to the failure: {_format_inputs(failure.inputs)}.")
    else:
        parts.append("No harness inputs could be extracted from the trace.")
    parts.append(_COUNTEREXAMPLE_SCOPE)
    return " ".join(parts)


def _explain_insufficient_unwind(failure: FailureReport, where: str, unwind: int | None) -> str:
    unwound = "recursion" if property_class(failure.property_id, failure.description) == "recursion" else "loop"
    bound = f"the unwind bound {unwind}" if unwind is not None else "the unwind bound"
    parts = [
        f"CBMC's unwinding assertion {failure.property_id or '?'} for the {unwound}{where} failed: some execution "
        f"allowed by the harness needs more iterations than {bound} permits."
    ]
    if failure.inputs:
        parts.append(f"For example: {_format_inputs(failure.inputs)}.")
    parts.append(_UNWIND_CONSEQUENCE)
    return " ".join(parts)


def explain_counterexample(failure: FailureReport, *, unwind: int | None = None) -> str:
    where = f" at {failure.location.short()}" if failure.location else ""
    if failure.kind == "counterexample":
        return _explain_violation(failure, where)
    if failure.kind == "unwind":
        return _explain_insufficient_unwind(failure, where, unwind)
    if failure.kind == "vacuity":
        return f"{failure.description}{where}. {_VACUITY_CONSEQUENCE}"
    rule = f" ({failure.property_id})" if failure.property_id else ""
    return f"{failure.tool} reported a {failure.kind} failure{rule}{where}: {failure.description}"
