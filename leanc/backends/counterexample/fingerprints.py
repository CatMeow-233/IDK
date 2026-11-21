from __future__ import annotations

import os
import re
from typing import Any

from leanc.backends.counterexample.locations import relative_file
from leanc.backends.counterexample.properties import (
    CBMC_TOOL,
    MAX_QUALIFIER_CHARS,
    VACUITY_ID_PREFIX,
    property_class,
    stable_qualifier,
)
from leanc.foundation.models import FailureReport

UNKNOWN_PART = "?"
NO_CLASS = "-"
CBMC_TRACE_KINDS = ("counterexample", "unwind")

_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]+")
_SANITIZER_PID_RE = re.compile(r"==\d+==")
_LONG_NUMBER_RE = re.compile(r"\b\d{5,}\b")
_ABSOLUTE_DIRECTORY_RE = re.compile(r"(?:/[^\s:/'\"()<>]+)+/")
_COUNTER_SUFFIX_RE = re.compile(r"(?:[.#]\d+)+$")


def _first_line(message: Any) -> str:
    text = str(message or "").strip()
    return text.splitlines()[0] if text else ""


def _mask_run_specific_noise(message: Any) -> str:
    text = _SANITIZER_PID_RE.sub("", _first_line(message))
    text = _ADDRESS_RE.sub("0x?", text)
    text = _ABSOLUTE_DIRECTORY_RE.sub("", text)
    text = _LONG_NUMBER_RE.sub("N", text)
    return " ".join(text.split())[:MAX_QUALIFIER_CHARS]


def _location_parts(location: Any) -> tuple[str, str, str]:
    file = line = function = UNKNOWN_PART
    if location is None:
        return file, line, function
    location_file = getattr(location, "file", None)
    if location_file:
        file = relative_file(str(location_file)) or UNKNOWN_PART
        if os.path.isabs(file):
            file = _ABSOLUTE_DIRECTORY_RE.sub("", file) or file
    if getattr(location, "line", None) is not None:
        line = str(location.line)
    if getattr(location, "function", None):
        function = str(location.function)
    return file, line, function


def _report_id(failure: FailureReport) -> str:
    return str(getattr(failure, "id", "") or "")


def _vacuity_qualifier(failure: FailureReport, description: str) -> str:
    report_id = _report_id(failure)
    if VACUITY_ID_PREFIX in report_id:
        return report_id.split(VACUITY_ID_PREFIX, 1)[1]
    return _mask_run_specific_noise(description)


def _distinguish_unlocated_by_report_id(failure: FailureReport, qualifier: str) -> str:
    report_id = _COUNTER_SUFFIX_RE.sub("", _report_id(failure))
    if not report_id:
        return qualifier
    return f"{report_id}: {qualifier}" if qualifier else report_id


def _class_and_qualifier(failure: FailureReport, tool: str, kind: str, file: str) -> tuple[str, str]:
    raw_property_id = getattr(failure, "property_id", None)
    property_id = str(raw_property_id) if raw_property_id else None
    description = str(getattr(failure, "description", None) or "")
    if tool == CBMC_TOOL and kind in CBMC_TRACE_KINDS:
        property_cls = property_class(property_id, description)
        return property_cls, "" if kind == "unwind" else stable_qualifier(property_cls, description)
    if kind == "vacuity":
        return "vacuity", _vacuity_qualifier(failure, description)
    property_cls = _COUNTER_SUFFIX_RE.sub("", property_id) if property_id else NO_CLASS
    qualifier = _mask_run_specific_noise(description)
    if file == UNKNOWN_PART:
        qualifier = _distinguish_unlocated_by_report_id(failure, qualifier)
    return property_cls, qualifier


def fingerprint(failure: FailureReport) -> str:
    tool = str(getattr(failure, "tool", None) or UNKNOWN_PART).strip() or UNKNOWN_PART
    kind = str(getattr(failure, "kind", None) or UNKNOWN_PART)
    file, line, function = _location_parts(getattr(failure, "location", None))
    property_cls, qualifier = _class_and_qualifier(failure, tool, kind, file)
    identity = f"{tool}:{kind}:{property_cls or NO_CLASS}@{file}:{line}:{function}"
    if qualifier:
        identity += f" [{qualifier}]"
    return identity
