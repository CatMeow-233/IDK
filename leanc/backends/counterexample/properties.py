from __future__ import annotations

import re
from typing import Any

from leanc.backends.counterexample.json_fields import as_dict

CBMC_TOOL = "cbmc"
REACH_PREFIX = "leanc_reach:"
VACUITY_ID_PREFIX = "cbmc:vacuity:"
UNWIND_PROPERTY_MARKER = ".unwind."
RECURSION_PROPERTY_SUFFIX = ".recursion"
LOOP_UNWINDING_DESCRIPTION = "unwinding assertion"
RECURSION_UNWINDING_DESCRIPTION = "recursion unwinding assertion"
HARNESS_ASSERTION_CLASS = "assertion"
EXPRESSION_SEPARATOR = " in "
MAX_QUALIFIER_CHARS = 120

_PROPERTY_ID_RE = re.compile(r"^(?P<func>.+?)\.(?P<cls>[A-Za-z_][A-Za-z0-9_-]*)\.(?P<num>\d+)$")


def is_unwinding_property(result: Any) -> bool:
    fields = as_dict(result)
    property_id = str(fields.get("property") or "")
    description = str(fields.get("description") or "").lower()
    return (
        UNWIND_PROPERTY_MARKER in property_id
        or property_id.endswith(RECURSION_PROPERTY_SUFFIX)
        or description.startswith((LOOP_UNWINDING_DESCRIPTION, RECURSION_UNWINDING_DESCRIPTION))
    )


def is_reach_probe(result: Any) -> bool:
    return str(as_dict(result).get("description") or "").startswith(REACH_PREFIX)


def _class_from_description(description: str) -> str:
    if "dereference failure" in description:
        return "pointer_dereference"
    if "division by zero" in description:
        return "division-by-zero"
    if "overflow" in description:
        return "overflow"
    if "array" in description and "bound" in description:
        return "array_bounds"
    if description.startswith((HARNESS_ASSERTION_CLASS, REACH_PREFIX)):
        return HARNESS_ASSERTION_CLASS
    if "shift" in description:
        return "undefined-shift"
    return "property"


def property_class(property_id: str | None, description: str | None = None) -> str:
    identifier = property_id or ""
    lowered = (description or "").lower()
    if UNWIND_PROPERTY_MARKER in identifier or lowered.startswith(LOOP_UNWINDING_DESCRIPTION):
        return "unwind"
    if identifier.endswith(RECURSION_PROPERTY_SUFFIX) or lowered.startswith(RECURSION_UNWINDING_DESCRIPTION):
        return "recursion"
    match = _PROPERTY_ID_RE.match(identifier)
    if match:
        return match.group("cls")
    return _class_from_description(lowered)


def stable_qualifier(property_cls: str, description: str | None) -> str:
    text = " ".join(str(description or "").split())
    if property_cls != HARNESS_ASSERTION_CLASS and EXPRESSION_SEPARATOR in text:
        text = text.split(EXPRESSION_SEPARATOR, 1)[0]
    return text[:MAX_QUALIFIER_CHARS]
