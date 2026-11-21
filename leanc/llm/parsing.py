"""Extraction and validation of model JSON answers: tolerates fences, prose and raw newlines, never guesses."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from enum import Enum
from typing import Any

import yaml

from leanc.foundation.errors import ProviderError, WorkspaceError
from leanc.foundation.redact import redact
from leanc.foundation.workspace import normalize_relpath
from leanc.llm.base import GeneratedCode, RepairProposal, SpecDraft

MAX_RESPONSE_CHARS = 4_000_000
MAX_BRACE_POSITIONS_SCANNED = 2_000
BYTE_ORDER_MARK = "﻿"

_FENCE_RE = re.compile(r"(?P<fence>`{3,}|~{3,})[ \t]*(?P<tag>[A-Za-z0-9_+.-]*)[^\n]*\n(?P<body>.*?)(?P=fence)", re.S)
_JSON_TAGS = {"json", "json5", "jsonc", "javascript", "js"}
_DECODER_TOLERATING_RAW_CONTROL_CHARS = json.JSONDecoder(strict=False)


class _Decoding(Enum):
    NOT_JSON = "the candidate text is not one JSON document"


def excerpt(text: str, limit: int = 300) -> str:
    """Short, redacted, single-line excerpt of a response for error messages."""
    s = redact(text or "").strip().replace("\r", "")
    s = re.sub(r"\s+", " ", s)
    return s if len(s) <= limit else s[:limit] + "..."


def _decode_document(candidate: str) -> Any:
    try:
        return _DECODER_TOLERATING_RAW_CONTROL_CHARS.decode(candidate)
    except ValueError:
        return _Decoding.NOT_JSON


def _fenced_blocks(text: str) -> list[str]:
    json_tagged, untagged, other_tagged = [], [], []
    for match in _FENCE_RE.finditer(text):
        tag = match.group("tag").lower()
        body = match.group("body").strip()
        if not body:
            continue
        if tag in _JSON_TAGS:
            json_tagged.append(body)
        elif not tag:
            untagged.append(body)
        else:
            other_tagged.append(body)
    return json_tagged + untagged + other_tagged


def _embedded_top_level_objects(text: str) -> Iterator[dict]:
    pos = 0
    attempts = 0
    while pos < len(text) and attempts < MAX_BRACE_POSITIONS_SCANNED:
        brace = text.find("{", pos)
        if brace < 0:
            return
        attempts += 1
        try:
            obj, object_end = _DECODER_TOLERATING_RAW_CONTROL_CHARS.raw_decode(text, brace)
        except ValueError:
            pos = brace + 1
            continue
        if isinstance(obj, dict):
            yield obj
        pos = max(object_end, brace + 1)


def _candidates(body: str) -> Iterator[Any]:
    if body.startswith("{"):
        yield _decode_document(body)
    for block in _fenced_blocks(body):
        yield _decode_document(block)
    yield from _embedded_top_level_objects(body)


def _answered_with_json_array(body: str) -> bool:
    return body.startswith("[") and isinstance(_decode_document(body), list)


def extract_json_object(text: str, *, expect_keys: Iterable[str] = ()) -> dict[str, Any]:
    """The first candidate object (whole text, fences, embedded) having all expect_keys, else the first object."""
    if not isinstance(text, str):
        raise ProviderError(f"model response is not text (got {type(text).__name__})")
    if len(text) > MAX_RESPONSE_CHARS:
        raise ProviderError(f"model response too large ({len(text)} characters > {MAX_RESPONSE_CHARS})")
    body = text.lstrip(BYTE_ORDER_MARK).strip()
    if not body:
        raise ProviderError("model response is empty (expected a JSON object)")
    keys = tuple(expect_keys)
    first: dict[str, Any] | None = None
    for candidate in _candidates(body):
        if not isinstance(candidate, dict):
            continue
        if all(k in candidate for k in keys):
            return candidate
        if first is None:
            first = candidate
    if first is not None:
        return first
    if _answered_with_json_array(body):
        raise ProviderError("model answered with a JSON array; expected a JSON object")
    raise ProviderError(f"no JSON object found in model response: {excerpt(body)!r}")


def check_keys(
    obj: dict[str, Any],
    *,
    required: Iterable[str] = (),
    optional: Iterable[str] = (),
    strict: bool = False,
    where: str = "response",
) -> list[str]:
    """Check that the required keys are present and return the unknown ones (an error when strict)."""
    if not isinstance(obj, dict):
        raise ProviderError(f"{where}: expected a JSON object, got {type(obj).__name__}")
    req = list(required)
    missing = [k for k in req if k not in obj]
    if missing:
        raise ProviderError(f"{where}: missing required key(s) {missing}; got {sorted(obj)}")
    known = set(req) | set(optional)
    unknown = sorted(k for k in obj if k not in known)
    if strict and unknown:
        raise ProviderError(f"{where}: unexpected key(s) {unknown}; allowed: {sorted(known)}")
    return unknown


def expect_str(
    obj: dict[str, Any],
    key: str,
    *,
    required: bool = True,
    default: str = "",
    allow_empty: bool = True,
    where: str = "response",
) -> str:
    """``obj[key]`` as a string (a list of strings joined by newlines; null/absent gives default unless required)."""
    if key not in obj or obj[key] is None:
        if required:
            raise ProviderError(f"{where}: missing required string {key!r}")
        return default
    value = obj[key]
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        value = "\n".join(value)
    if not isinstance(value, str):
        raise ProviderError(f"{where}: {key!r} must be a string, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise ProviderError(f"{where}: {key!r} must not be empty")
    return value


def strip_code_fence(content: str) -> str:
    """Remove one markdown fence wrapping an entire value (models sometimes fence code inside a JSON string)."""
    s = content.strip()
    match = re.fullmatch(r"(`{3,}|~{3,})[^\n]*\n(.*?)\n?\1", s, re.S)
    if match:
        return match.group(2) + "\n"
    return content


def _normalise_source(content: str) -> str:
    content = strip_code_fence(content).replace("\r\n", "\n")
    if content and not content.endswith("\n"):
        content += "\n"
    return content


def _normalised_or_verbatim_for_policy_rejection(path: str) -> str:
    try:
        return normalize_relpath(path.strip())
    except WorkspaceError:
        return path


def expect_files(
    obj: dict[str, Any],
    key: str = "files",
    *,
    required: bool = True,
    clean: bool = True,
    where: str = "response",
) -> dict[str, str] | None:
    """Validate a ``{relative path: full content}`` map (None when absent/empty and not required)."""
    value = obj.get(key)
    if value is None or value == {}:
        if required:
            raise ProviderError(f"{where}: {key!r} must be a non-empty object mapping file paths to contents")
        return None
    if not isinstance(value, dict):
        raise ProviderError(
            f"{where}: {key!r} must be an object mapping file paths to contents, got {type(value).__name__}"
        )
    out: dict[str, str] = {}
    for path, content in value.items():
        if not isinstance(path, str) or not path.strip():
            raise ProviderError(f"{where}: {key!r} has an empty file path")
        if not isinstance(content, str):
            raise ProviderError(f"{where}: content of {path!r} must be a string, got {type(content).__name__}")
        norm = _normalised_or_verbatim_for_policy_rejection(path)
        if norm in out:
            raise ProviderError(f"{where}: file {norm!r} given more than once")
        out[norm] = _normalise_source(content) if clean else content
    return out


CODEGEN_KEYS = ("files",)
CODEGEN_OPTIONAL = ("notes",)
SPEC_DRAFT_KEYS = ("spec_yaml",)
SPEC_DRAFT_OPTIONAL = ("harness_c", "tests_c", "notes")
REPAIR_KEYS = ("root_cause", "rationale")
REPAIR_OPTIONAL = ("files", "patch", "note")


def _with_ignored_keys_note(notes: str, unknown: list[str]) -> str:
    if not unknown:
        return notes
    return (notes + "\n" if notes else "") + f"(ignored unexpected keys: {', '.join(unknown)})"


def generated_code_from_obj(obj: dict[str, Any], *, strict: bool = False, raw: str = "") -> GeneratedCode:
    where = "code generation response"
    unknown = check_keys(obj, required=CODEGEN_KEYS, optional=CODEGEN_OPTIONAL, strict=strict, where=where)
    files = expect_files(obj, "files", required=True, clean=not strict, where=where) or {}
    notes = _with_ignored_keys_note(expect_str(obj, "notes", required=False, where=where), unknown)
    return GeneratedCode(files=files, notes=notes, raw_response=raw)


def _model_sent_spec_as_mapping(spec_value: Any, strict: bool) -> bool:
    return isinstance(spec_value, dict) and not strict


def spec_draft_from_obj(obj: dict[str, Any], *, strict: bool = False, raw: str = "") -> SpecDraft:
    where = "spec draft response"
    unknown = check_keys(obj, required=SPEC_DRAFT_KEYS, optional=SPEC_DRAFT_OPTIONAL, strict=strict, where=where)
    spec_value = obj.get("spec_yaml")
    if _model_sent_spec_as_mapping(spec_value, strict):
        spec_yaml = yaml.safe_dump(spec_value, sort_keys=False, allow_unicode=True, width=100)
    else:
        spec_yaml = strip_code_fence(expect_str(obj, "spec_yaml", required=True, allow_empty=False, where=where))
    harness = expect_str(obj, "harness_c", required=False, where=where) or None
    tests = expect_str(obj, "tests_c", required=False, where=where) or None
    notes = _with_ignored_keys_note(expect_str(obj, "notes", required=False, where=where), unknown)
    if not strict:
        harness = _normalise_source(harness) if harness else None
        tests = _normalise_source(tests) if tests else None
    return SpecDraft(
        spec_yaml=spec_yaml,
        harness_c=harness,
        tests_c=tests,
        notes=notes,
        raw_response=raw,
    )


def repair_proposal_from_obj(obj: dict[str, Any], *, strict: bool = False, raw: str = "") -> RepairProposal:
    """Validate a repair answer whose change is in exactly one of files/patch (neither: no change, with a note)."""
    where = "repair response"
    if strict:
        check_keys(obj, required=REPAIR_KEYS, optional=REPAIR_OPTIONAL, strict=True, where=where)
    else:
        check_keys(obj, optional=REPAIR_KEYS + REPAIR_OPTIONAL, where=where)
    root_cause = expect_str(obj, "root_cause", required=strict, where=where)
    rationale = expect_str(obj, "rationale", required=strict, where=where)
    files = expect_files(obj, "files", required=False, clean=not strict, where=where)
    patch_value = obj.get("patch")
    if patch_value is not None and not isinstance(patch_value, str):
        raise ProviderError(f"{where}: 'patch' must be a unified diff string, got {type(patch_value).__name__}")
    patch = patch_value if patch_value and patch_value.strip() else None
    if patch is not None and not strict:
        patch = strip_code_fence(patch)
    if files and patch:
        raise ProviderError(f"{where}: give either 'files' or 'patch', not both")
    note = expect_str(obj, "note", required=False, where=where)
    if not files and not patch:
        if strict:
            raise ProviderError(f"{where}: must contain a non-empty 'files' object or a non-empty 'patch' string")
        note = note or "provider response contained neither 'files' nor 'patch' (no change proposed)"
    return RepairProposal(
        root_cause=root_cause, rationale=rationale, files=files, patch=patch, raw_response=raw, note=note
    )


def parse_code_response(text: str) -> GeneratedCode:
    return generated_code_from_obj(extract_json_object(text, expect_keys=CODEGEN_KEYS), raw=text)


def parse_spec_draft_response(text: str) -> SpecDraft:
    return spec_draft_from_obj(extract_json_object(text, expect_keys=SPEC_DRAFT_KEYS), raw=text)


def parse_repair_response(text: str) -> RepairProposal:
    return repair_proposal_from_obj(extract_json_object(text, expect_keys=("root_cause",)), raw=text)


__all__ = [
    "MAX_RESPONSE_CHARS",
    "check_keys",
    "excerpt",
    "expect_files",
    "expect_str",
    "extract_json_object",
    "generated_code_from_obj",
    "parse_code_response",
    "parse_repair_response",
    "parse_spec_draft_response",
    "repair_proposal_from_obj",
    "spec_draft_from_obj",
    "strip_code_fence",
]
