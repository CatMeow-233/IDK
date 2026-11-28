from __future__ import annotations

import re
from dataclasses import dataclass, field

from leanc.backends.counterexample.json_fields import as_list

MODELLED_BODILESS_PREFIXES = ("nondet_", "__CPROVER")
MODELLED_BODILESS_NAMES = frozenset({"__builtin_prefetch", "__builtin_expect"})
ASSIGNMENT_STEP = "assignment"
MAX_NAMED_VIOLATIONS = 3
UNMODELLED_CONSEQUENCE = (
    "their return values are arbitrary, their writes through pointer arguments are dropped and their own "
    "memory accesses are not checked"
)

UNDECLARED_CONSEQUENCE = (
    "CBMC types an undeclared function as returning int, so each call yields only int values converted to the "
    "type the caller uses (a nondet_* input then covers only part of its type's range); declare the function "
    "(leanc.h declares the common nondet_* inputs)"
)

_NO_BODY_RE = re.compile(r"no body for (?:function|callee) '?([A-Za-z_][\w$]*)'?")
_UNDECLARED_RE = re.compile(r"function '([A-Za-z_][\w$]*)' is not declared")
_RETURN_TEMPORARY_RE = re.compile(r"^(?:return_value_|goto_symex\$\$return_value\$\$)(?P<function>[A-Za-z_]\w*)")
_RETURN_VALUE_IN_TEXT_RE = re.compile(r"\breturn_value_(?P<function>[A-Za-z_]\w*)")
NO_BODY_PROPERTY_MARKER = ".no-body."


def is_modelled_bodiless(function: str) -> bool:
    return function.startswith(MODELLED_BODILESS_PREFIXES) or function in MODELLED_BODILESS_NAMES


def bodiless_function(warning_text: str) -> str | None:
    match = _NO_BODY_RE.search(warning_text)
    return match.group(1) if match else None


def no_body_property_function(result: dict) -> str | None:
    prop = str(result.get("property") or "")
    if NO_BODY_PROPERTY_MARKER not in prop:
        return None
    return bodiless_function(str(result.get("description") or ""))


def undecided_explained_by_unmodelled(result: dict, unmodelled: frozenset[str], no_body_callers: set[str]) -> bool:
    if not unmodelled:
        return False
    description = str(result.get("description") or "")
    if any(match.group("function") in unmodelled for match in _RETURN_VALUE_IN_TEXT_RE.finditer(description)):
        return True
    location = result.get("sourceLocation")
    if isinstance(location, dict) and location.get("function") in no_body_callers:
        return True
    return False


def undeclared_function(warning_text: str) -> str | None:
    match = _UNDECLARED_RE.search(warning_text)
    return match.group(1) if match else None


def undeclared_reason(functions: list[str]) -> str:
    return f"the code calls undeclared function(s) {call_list(sorted(functions))}: " + UNDECLARED_CONSEQUENCE


def _returned_function(step: dict) -> str | None:
    if step.get("stepType") != ASSIGNMENT_STEP:
        return None
    match = _RETURN_TEMPORARY_RE.match(str(step.get("lhs") or ""))
    return match.group("function") if match else None


def unmodelled_return_values_in_trace(result: dict, unmodelled: frozenset[str]) -> list[str]:
    found: list[str] = []
    for step in as_list(result.get("trace")):
        function = _returned_function(step) if isinstance(step, dict) else None
        if function is not None and function in unmodelled and function not in found:
            found.append(function)
    return found


@dataclass(frozen=True)
class DependentViolation:
    result: dict
    functions: tuple[str, ...]


@dataclass
class ViolationSplit:
    real: list[dict] = field(default_factory=list)
    dependent: list[DependentViolation] = field(default_factory=list)


def split_violations(violated: list[dict], unmodelled: frozenset[str]) -> ViolationSplit:
    split = ViolationSplit()
    for result in violated:
        callee = no_body_property_function(result)
        if callee is not None and callee in unmodelled:
            split.dependent.append(DependentViolation(result, (callee,)))
            continue
        functions = unmodelled_return_values_in_trace(result, unmodelled)
        if functions:
            split.dependent.append(DependentViolation(result, tuple(functions)))
        else:
            split.real.append(result)
    return split


def call_list(functions: list[str] | tuple[str, ...]) -> str:
    return ", ".join(f"{function}()" for function in functions)


def unmodelled_reason(unmodelled: list[str], dependent: list[DependentViolation]) -> str:
    reason = (
        f"the code calls body-less function(s) {call_list(sorted(unmodelled))} that CBMC does not model: "
        + UNMODELLED_CONSEQUENCE
    )
    if dependent:
        named = ", ".join(str(item.result.get("property")) for item in dependent[:MAX_NAMED_VIOLATIONS])
        more = f" and {len(dependent) - MAX_NAMED_VIOLATIONS} more" if len(dependent) > MAX_NAMED_VIOLATIONS else ""
        reason += (
            f"; {len(dependent)} reported violation(s) use their arbitrary return values and are not "
            f"counterexamples: {named}{more}"
        )
    return reason
