"""Robust extraction and validation of the JSON objects models answer with.

Models are asked to answer with *only* a JSON object, but in practice they
wrap it in markdown fences, prepend prose ("Here is the fix:"), or emit raw
newlines inside strings.  :func:`extract_json_object` tolerates exactly those
quirks and nothing more: it never "repairs" broken JSON by guessing, because a
silently mis-parsed response would be worse than a clear :class:`ProviderError`.

The ``*_from_obj`` validators are shared by the HTTP provider (``strict=False``:
unknown keys are ignored, missing explanatory fields default to "") and the
mock provider (``strict=True``: fixtures must match the documented format
exactly, so a typo in a fixture is reported instead of silently ignored).
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

import yaml

from leanc.core.errors import ProviderError, WorkspaceError
from leanc.core.redact import redact
from leanc.core.workspace import normalize_relpath
from leanc.llm.base import GeneratedCode, RepairProposal, SpecDraft

#: Responses larger than this are rejected outright (protects the scanner and
#: everything downstream from pathological output).
MAX_RESPONSE_CHARS = 4_000_000
#: At most this many '{' positions are tried by the fallback scanner.
MAX_SCAN_ATTEMPTS = 2_000

_FENCE_RE = re.compile(r"(?P<fence>`{3,}|~{3,})[ \t]*(?P<tag>[A-Za-z0-9_+.-]*)[^\n]*\n(?P<body>.*?)(?P=fence)", re.S)
_JSON_TAGS = {"json", "json5", "jsonc", "javascript", "js"}
# strict=False lets control characters (raw newlines/tabs) appear inside
# strings - the most common way models break otherwise valid JSON when they
# embed source code.
_DECODER = json.JSONDecoder(strict=False)


def excerpt(text: str, limit: int = 300) -> str:
    """Short, redacted, single-line excerpt of a response for error messages."""
    s = redact(text or "").strip().replace("\r", "")
    s = re.sub(r"\s+", " ", s)
    return s if len(s) <= limit else s[:limit] + "..."


def _try_loads(candidate: str) -> Any:
    try:
        return _DECODER.decode(candidate)
    except ValueError:
        return None


def _fenced_blocks(text: str) -> list[str]:
    """Bodies of markdown code fences: json-tagged first, then untagged, then
    the rest (a model sometimes tags JSON as ``text``)."""
    tagged, untagged, other = [], [], []
    for m in _FENCE_RE.finditer(text):
        tag = m.group("tag").lower()
        body = m.group("body").strip()
        if not body:
            continue
        if tag in _JSON_TAGS:
            tagged.append(body)
        elif not tag:
            untagged.append(body)
        else:
            other.append(body)
    return tagged + untagged + other


def _scan_objects(text: str) -> Iterable[dict]:
    """Yield top-level JSON objects embedded anywhere in ``text``.

    ``raw_decode`` is a real JSON parser, so braces inside strings (C code in
    a ``files`` value, say) are handled correctly.  After a successful decode
    the scan continues *after* that object, so nested objects are not yielded
    separately."""
    pos = 0
    attempts = 0
    n = len(text)
    while pos < n and attempts < MAX_SCAN_ATTEMPTS:
        i = text.find("{", pos)
        if i < 0:
            return
        attempts += 1
        try:
            obj, end = _DECODER.raw_decode(text, i)
        except ValueError:
            pos = i + 1
            continue
        if isinstance(obj, dict):
            yield obj
        pos = max(end, i + 1)


def extract_json_object(text: str, *, expect_keys: Iterable[str] = ()) -> dict[str, Any]:
    """Return the JSON object a model answered with.

    Tries, in order: the whole (stripped) text; markdown code fences (json
    first); every top-level ``{...}`` embedded in prose.  If ``expect_keys`` is
    given, the first candidate containing *all* of them wins (so a small JSON
    example quoted in leading prose does not shadow the real answer);
    otherwise the first object found.  Raises :class:`ProviderError` if no
    JSON object can be found.
    """
    if not isinstance(text, str):
        raise ProviderError(f"model response is not text (got {type(text).__name__})")
    if len(text) > MAX_RESPONSE_CHARS:
        raise ProviderError(f"model response too large ({len(text)} characters > {MAX_RESPONSE_CHARS})")
    body = text.lstrip("﻿").strip()
    if not body:
        raise ProviderError("model response is empty (expected a JSON object)")
    keys = tuple(expect_keys)
    first: dict[str, Any] | None = None

    def consider(obj: Any) -> dict[str, Any] | None:
        nonlocal first
        if not isinstance(obj, dict):
            return None
        if all(k in obj for k in keys):
            return obj
        if first is None:
            first = obj
        return None

    if body.startswith("{"):
        hit = consider(_try_loads(body))
        if hit is not None:
            return hit
    for block in _fenced_blocks(body):
        hit = consider(_try_loads(block))
        if hit is not None:
            return hit
    for obj in _scan_objects(body):
        hit = consider(obj)
        if hit is not None:
            return hit
    if first is not None:
        return first
    if body.startswith("[") and isinstance(_try_loads(body), list):
        raise ProviderError("model answered with a JSON array; expected a JSON object")
    raise ProviderError(f"no JSON object found in model response: {excerpt(body)!r}")


# ------------------------------------------------------------------ field validators
def check_keys(
    obj: dict[str, Any],
    *,
    required: Iterable[str] = (),
    optional: Iterable[str] = (),
    strict: bool = False,
    where: str = "response",
) -> list[str]:
    """Check presence of ``required`` keys; return the unknown keys.

    With ``strict`` unknown keys are an error (used for fixtures)."""
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
    """Return ``obj[key]`` as a string (a list of strings is joined by
    newlines; ``null``/absent gives ``default`` unless ``required``)."""
    if key not in obj or obj[key] is None:
        if required:
            raise ProviderError(f"{where}: missing required string {key!r}")
        return default
    v = obj[key]
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        v = "\n".join(v)
    if not isinstance(v, str):
        raise ProviderError(f"{where}: {key!r} must be a string, got {type(v).__name__}")
    if not allow_empty and not v.strip():
        raise ProviderError(f"{where}: {key!r} must not be empty")
    return v


def strip_code_fence(content: str) -> str:
    """Remove one markdown fence wrapping an entire value (models sometimes
    put ```c ... ``` inside a JSON string)."""
    s = content.strip()
    m = re.fullmatch(r"(`{3,}|~{3,})[^\n]*\n(.*?)\n?\1", s, re.S)
    if m:
        return m.group(2) + "\n"
    return content


def _normalise_source(content: str) -> str:
    content = strip_code_fence(content).replace("\r\n", "\n")
    if content and not content.endswith("\n"):
        content += "\n"
    return content


def expect_files(
    obj: dict[str, Any],
    key: str = "files",
    *,
    required: bool = True,
    clean: bool = True,
    where: str = "response",
) -> dict[str, str] | None:
    """Validate a ``{relative path: full content}`` map.

    Paths are normalised (``./src/a.c`` -> ``src/a.c``) when they are valid
    workspace-relative paths; invalid ones (absolute, ``..``) are kept verbatim
    so that the generation/repair policy rejects them visibly instead of the
    parser silently fixing an attempted escape.  With ``clean`` (model output)
    a wrapping markdown fence is removed, CRLF normalised and a final newline
    ensured; fixtures (``clean=False``) are returned byte-for-byte.  Returns
    ``None`` when the key is absent/null/empty and not required."""
    v = obj.get(key)
    if v is None or v == {}:
        if required:
            raise ProviderError(f"{where}: {key!r} must be a non-empty object mapping file paths to contents")
        return None
    if not isinstance(v, dict):
        raise ProviderError(f"{where}: {key!r} must be an object mapping file paths to contents, got {type(v).__name__}")
    out: dict[str, str] = {}
    for path, content in v.items():
        if not isinstance(path, str) or not path.strip():
            raise ProviderError(f"{where}: {key!r} has an empty file path")
        if not isinstance(content, str):
            raise ProviderError(f"{where}: content of {path!r} must be a string, got {type(content).__name__}")
        try:
            norm = normalize_relpath(path.strip())
        except WorkspaceError:
            norm = path
        if norm in out:
            raise ProviderError(f"{where}: file {norm!r} given more than once")
        out[norm] = _normalise_source(content) if clean else content
    return out


# ------------------------------------------------------------------ typed parsers
CODEGEN_KEYS = ("files",)
CODEGEN_OPTIONAL = ("notes",)
SPEC_DRAFT_KEYS = ("spec_yaml",)
SPEC_DRAFT_OPTIONAL = ("harness_c", "tests_c", "notes")
REPAIR_KEYS = ("root_cause", "rationale")
REPAIR_OPTIONAL = ("files", "patch", "note")


def generated_code_from_obj(obj: dict[str, Any], *, strict: bool = False, raw: str = "") -> GeneratedCode:
    where = "code generation response"
    unknown = check_keys(obj, required=CODEGEN_KEYS, optional=CODEGEN_OPTIONAL, strict=strict, where=where)
    files = expect_files(obj, "files", required=True, clean=not strict, where=where) or {}
    notes = expect_str(obj, "notes", required=False, where=where)
    if unknown:
        notes = (notes + "\n" if notes else "") + f"(ignored unexpected keys: {', '.join(unknown)})"
    return GeneratedCode(files=files, notes=notes, raw_response=raw)


def spec_draft_from_obj(obj: dict[str, Any], *, strict: bool = False, raw: str = "") -> SpecDraft:
    where = "spec draft response"
    unknown = check_keys(obj, required=SPEC_DRAFT_KEYS, optional=SPEC_DRAFT_OPTIONAL, strict=strict, where=where)
    spec_val = obj.get("spec_yaml")
    if isinstance(spec_val, dict) and not strict:
        # The model emitted the spec as a JSON object instead of YAML text:
        # unambiguous, so serialise it rather than failing the request.
        spec_yaml = yaml.safe_dump(spec_val, sort_keys=False, allow_unicode=True, width=100)
    else:
        spec_yaml = strip_code_fence(expect_str(obj, "spec_yaml", required=True, allow_empty=False, where=where))
    harness = expect_str(obj, "harness_c", required=False, where=where) or None
    tests = expect_str(obj, "tests_c", required=False, where=where) or None
    notes = expect_str(obj, "notes", required=False, where=where)
    if unknown:
        notes = (notes + "\n" if notes else "") + f"(ignored unexpected keys: {', '.join(unknown)})"
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
    """Validate ``{"root_cause", "rationale", "files"}`` or ``{..., "patch"}``.

    Exactly one of ``files``/``patch`` may carry a change (empty values count
    as absent).  A proposal with neither is returned with an explanatory
    ``note``; the repair loop records it as a failed round."""
    where = "repair response"
    if strict:
        check_keys(obj, required=REPAIR_KEYS, optional=REPAIR_OPTIONAL, strict=True, where=where)
    else:
        check_keys(obj, optional=REPAIR_KEYS + REPAIR_OPTIONAL, where=where)
    root_cause = expect_str(obj, "root_cause", required=strict, where=where)
    rationale = expect_str(obj, "rationale", required=strict, where=where)
    files = expect_files(obj, "files", required=False, clean=not strict, where=where)
    patch_val = obj.get("patch")
    if patch_val is not None and not isinstance(patch_val, str):
        raise ProviderError(f"{where}: 'patch' must be a unified diff string, got {type(patch_val).__name__}")
    patch = patch_val if patch_val and patch_val.strip() else None
    if patch is not None and not strict:
        patch = strip_code_fence(patch)
    if files and patch:
        raise ProviderError(f"{where}: give either 'files' or 'patch', not both")
    note = expect_str(obj, "note", required=False, where=where)
    if not files and not patch:
        if strict:
            raise ProviderError(f"{where}: must contain a non-empty 'files' object or a non-empty 'patch' string")
        note = note or "provider response contained neither 'files' nor 'patch' (no change proposed)"
    return RepairProposal(root_cause=root_cause, rationale=rationale, files=files, patch=patch, raw_response=raw, note=note)


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
