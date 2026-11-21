from __future__ import annotations

import re
from dataclasses import dataclass, field

from leanc.backends.counterexample.json_fields import as_dict, as_list

MODELLED_BODILESS_PREFIXES = ("nondet_", "__CPROVER")
MODELLED_BODILESS_NAMES = frozenset({"__builtin_prefetch", "__builtin_expect"})
FUNCTION_CALL_STEP = "function-call"
ASSIGNMENT_STEP = "assignment"
MAX_NAMED_VIOLATIONS = 3
UNMODELLED_CONSEQUENCE = (
    "their return values are arbitrary, their writes through pointer arguments are dropped and their own "
    "memory accesses are not checked"
)

_NO_BODY_RE = re.compile(r"no body for (?:function|callee) '?([A-Za-z_][\w$]*)'?")
_RETURN_TEMPORARY_RE = re.compile(r"^(?:return_value_|goto_symex\$\$return_value\$\$)(?P<function>[A-Za-z_]\w*)")


def is_modelled_bodiless(function: str) -> bool:
    return function.startswith(MODELLED_BODILESS_PREFIXES) or function in MODELLED_BODILESS_NAMES


def bodiless_function(warning_text: str) -> str | None:
    match = _NO_BODY_RE.search(warning_text)
    return match.group(1) if match else None


def _called_function(step: dict) -> str | None:
    if step.get("stepType") != FUNCTION_CALL_STEP:
        return None
    identifier = as_dict(step.get("function")).get("identifier")
    return identifier if isinstance(identifier, str) else None


def _returned_function(step: dict) -> str | None:
    if step.get("stepType") != ASSIGNMENT_STEP:
        return None
    match = _RETURN_TEMPORARY_RE.match(str(step.get("lhs") or ""))
    return match.group("function") if match else None


def unmodelled_callees_in_trace(result: dict, unmodelled: frozenset[str]) -> list[str]:
    found: list[str] = []
    for step in as_list(result.get("trace")):
        if not isinstance(step, dict):
            continue
        for function in (_called_function(step), _returned_function(step)):
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
        functions = unmodelled_callees_in_trace(result, unmodelled)
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
            f"; {len(dependent)} reported violation(s) pass through them and are not counterexamples: {named}{more}"
        )
    return reason
