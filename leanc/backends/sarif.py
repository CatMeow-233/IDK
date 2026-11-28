from __future__ import annotations

import json
import os
import posixpath
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from leanc.backends.clang_diagnostics import relative_to_base
from leanc.foundation.models import Diagnostic, Location, PathStep, Severity

ANALYZER_TOOL = "clang-static-analyzer"
DEFAULT_LEVEL = "warning"
FALLBACK_MESSAGE = "static analysis finding"
MAX_RAW_RESULT_CHARS = 4000
TRUNCATION_MARKER = "...[truncated]"

_SEVERITY_BY_LEVEL: dict[str, Severity] = {"error": "error", "warning": "warning", "note": "note", "none": "info"}
_FUNCTION_LOCATION_KINDS = (None, "function", "member")
_MESSAGE_PLACEHOLDER_RE = re.compile(r"\{(\d+)\}")
_WINDOWS_URI_PATH_RE = re.compile(r"^/[A-Za-z]:/")
_WINDOWS_DRIVE_ABS_RE = re.compile(r"^[A-Za-z]:[/\\]")


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _non_empty_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _local_path_of_file_uri(uri: str) -> str:
    decoded = unquote(urlparse(uri).path)
    return decoded[1:] if _WINDOWS_URI_PATH_RE.match(decoded) else decoded


def _uri_to_path(uri: str, uri_base_id: str | None, base_ids: dict) -> str:
    if uri.startswith("file:"):
        return _local_path_of_file_uri(uri)
    path = unquote(uri)
    if not uri_base_id or os.path.isabs(path) or _WINDOWS_DRIVE_ABS_RE.match(path):
        return path
    base = _as_dict(base_ids.get(uri_base_id))
    base_uri = _non_empty_str(base.get("uri"))
    if base_uri is None:
        return path
    base_path = _local_path_of_file_uri(base_uri) if base_uri.startswith("file:") else unquote(base_uri)
    return posixpath.join(base_path, path)


def _literal_message(message: dict) -> str | None:
    for key in ("text", "markdown"):
        text = _non_empty_str(message.get(key))
        if text is not None:
            return text
    return None


def _templated_message(message: dict, rule: dict | None) -> str | None:
    message_id = message.get("id")
    if not isinstance(message_id, str) or not rule:
        return None
    template = _as_dict(_as_dict(rule.get("messageStrings")).get(message_id)).get("text")
    if not isinstance(template, str):
        return None
    arguments = [str(argument) for argument in _as_list(message.get("arguments"))]

    def fill(placeholder: re.Match) -> str:
        position = int(placeholder.group(1))
        return arguments[position] if position < len(arguments) else placeholder.group(0)

    return _MESSAGE_PLACEHOLDER_RE.sub(fill, template)


def _message_text(message: Any, rule: dict | None = None) -> str:
    fields = _as_dict(message)
    return _literal_message(fields) or _templated_message(fields, rule) or ""


class _SarifRun:
    def __init__(self, run: dict, base_dir: Path | None):
        self.base_dir = base_dir
        self.artifacts = _as_list(run.get("artifacts"))
        self.rules = _as_list(_as_dict(_as_dict(run.get("tool")).get("driver")).get("rules"))
        self.base_ids = _as_dict(run.get("originalUriBaseIds"))

    def rule_for(self, result: dict) -> dict:
        index = _as_int(result.get("ruleIndex"))
        if index is None:
            index = _as_int(_as_dict(result.get("rule")).get("index"))
        if index is not None and 0 <= index < len(self.rules):
            return _as_dict(self.rules[index])
        rule_id = result.get("ruleId") or _as_dict(result.get("rule")).get("id")
        if isinstance(rule_id, str):
            for rule in self.rules:
                if _as_dict(rule).get("id") == rule_id:
                    return _as_dict(rule)
        return {}

    def _artifact_file(self, artifact_location: dict) -> str | None:
        uri = artifact_location.get("uri")
        base_id = artifact_location.get("uriBaseId")
        if not isinstance(uri, str) or not uri:
            index = _as_int(artifact_location.get("index"))
            if index is not None and 0 <= index < len(self.artifacts):
                listed = _as_dict(_as_dict(self.artifacts[index]).get("location"))
                uri = listed.get("uri")
                base_id = base_id or listed.get("uriBaseId")
        if not isinstance(uri, str) or not uri:
            return None
        path = _uri_to_path(uri, base_id if isinstance(base_id, str) else None, self.base_ids)
        return relative_to_base(path, self.base_dir)

    def location(self, raw_location: Any) -> Location | None:
        sarif_location = _as_dict(raw_location)
        physical = _as_dict(sarif_location.get("physicalLocation"))
        file = self._artifact_file(_as_dict(physical.get("artifactLocation")))
        region = _as_dict(physical.get("region"))
        line = _as_int(region.get("startLine"))
        column = _as_int(region.get("startColumn"))
        function = _function_name(sarif_location)
        if file is None and line is None and function is None:
            return None
        return Location(file=file, line=line, column=column, function=function)


def _function_name(sarif_location: dict) -> str | None:
    for raw_logical in _as_list(sarif_location.get("logicalLocations")):
        logical = _as_dict(raw_logical)
        name = logical.get("fullyQualifiedName") or logical.get("name")
        if isinstance(name, str) and name and logical.get("kind") in _FUNCTION_LOCATION_KINDS:
            return name
    return None


def _rule_id(result: dict, rule: dict) -> str | None:
    rule_id = result.get("ruleId")
    if not isinstance(rule_id, str) or not rule_id:
        rule_id = _as_dict(result.get("rule")).get("id") or rule.get("id")
    return rule_id if isinstance(rule_id, str) and rule_id else None


def _severity(result: dict, rule: dict) -> Severity:
    level = result.get("level")
    if not isinstance(level, str):
        level = _as_dict(rule.get("defaultConfiguration")).get("level")
    return _SEVERITY_BY_LEVEL.get(level if isinstance(level, str) else DEFAULT_LEVEL, _SEVERITY_BY_LEVEL[DEFAULT_LEVEL])


def _code_flow_steps(run: _SarifRun, result: dict) -> list[PathStep]:
    steps: list[PathStep] = []
    for code_flow in _as_list(result.get("codeFlows")):
        for thread_flow in _as_list(_as_dict(code_flow).get("threadFlows")):
            for raw_step in _as_list(_as_dict(thread_flow).get("locations")):
                step = _as_dict(raw_step)
                step_location = _as_dict(step.get("location"))
                message = _message_text(step_location.get("message")) or _message_text(step.get("message"))
                steps.append(PathStep(location=run.location(step_location), message=message))
    return steps


def _raw_excerpt(result: dict) -> str:
    raw = json.dumps(result, sort_keys=True)
    if len(raw) > MAX_RAW_RESULT_CHARS:
        return raw[:MAX_RAW_RESULT_CHARS] + TRUNCATION_MARKER
    return raw


def _result_diagnostic(run: _SarifRun, result: dict, tool: str) -> Diagnostic:
    rule = run.rule_for(result)
    rule_id = _rule_id(result, rule)
    message = _message_text(result.get("message"), rule) or (
        _message_text(rule.get("shortDescription")) or (rule_id or FALLBACK_MESSAGE)
    )
    location = next(
        (found for found in map(run.location, _as_list(result.get("locations"))) if found is not None), None
    )
    path = _code_flow_steps(run, result)
    if location is None and path:
        location = path[-1].location
    return Diagnostic(
        tool=tool,
        rule_id=rule_id,
        severity=_severity(result, rule),
        message=message,
        location=location,
        path=path,
        raw=_raw_excerpt(result),
    )


def parse_sarif(data: dict, *, base_dir: Path | None = None, tool: str = ANALYZER_TOOL) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for raw_run in _as_list(_as_dict(data).get("runs")):
        sarif_run = _as_dict(raw_run)
        run = _SarifRun(sarif_run, base_dir)
        for result in _as_list(sarif_run.get("results")):
            if isinstance(result, dict):
                diagnostics.append(_result_diagnostic(run, result, tool))
    return diagnostics
