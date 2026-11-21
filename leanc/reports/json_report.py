from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import tempfile
from enum import Enum
from pathlib import Path, PurePath
from typing import Any, Iterable, TypeVar

from pydantic import BaseModel, ValidationError

from leanc.foundation.errors import LeanCError
from leanc.foundation.models import (
    ApprovalCheck,
    Diagnostic,
    FailureReport,
    RunRecord,
    StageResult,
    VerificationResult,
)
from leanc.foundation.redact import REDACTED, redact
from leanc.foundation.status import exit_code, is_success
from leanc.foundation.text import utf8_safe

log = logging.getLogger("leanc.reports")

REPORT_FILE_MODE = 0o644
MAX_VALIDATION_ERRORS_SHOWN = 3

TOOL_ORDER: tuple[str, ...] = (
    "clang",
    "clang-static-analyzer",
    "asan",
    "ubsan",
    "lsan",
    "test-oracle",
    "cbmc",
)
_TOOL_RANK = {tool: rank for rank, tool in enumerate(TOOL_ORDER)}

FINDING_SEVERITIES = frozenset({"error", "warning"})

_M = TypeVar("_M", bound=BaseModel)


def tool_sort_key(tool: str) -> tuple[int, str]:
    return (_TOOL_RANK.get(tool, len(TOOL_ORDER)), tool)


def sort_tools(tools: Iterable[str]) -> list[str]:
    return sorted({t for t in tools if t}, key=tool_sort_key)


def is_finding(d: Diagnostic) -> bool:
    return d.severity in FINDING_SEVERITIES


def detecting_tools(stage: StageResult) -> list[str]:
    return sort_tools([f.tool for f in stage.failures] + [d.tool for d in stage.diagnostics if is_finding(d)])


def invoked_tools(stage: StageResult) -> list[str]:
    seen: dict[str, None] = {}
    for inv in stage.invocations:
        seen.setdefault(inv.tool, None)
    return list(seen)


def _is_non_finite_float(obj: Any) -> bool:
    return isinstance(obj, float) and not math.isfinite(obj)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return _jsonable(obj.model_dump(mode="json"))
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, PurePath):
        return str(obj)
    if isinstance(obj, dict):
        return {str(_jsonable(k)): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted((_jsonable(v) for v in obj), key=lambda v: json.dumps(v, sort_keys=True, default=str))
    if _is_non_finite_float(obj):
        return str(obj)
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


def redact_data(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {redact(k) if isinstance(k, str) else k: redact_data(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_data(v) for v in obj]
    return obj


def _redact_numeric_secrets(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _redact_numeric_secrets(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_numeric_secrets(v) for v in obj]
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        text = json.dumps(obj)
        return REDACTED if redact(text) != text else obj
    return obj


def _serialise(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def _valid_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def _text_redaction_keeping_valid_json(text: str) -> str | None:
    redacted = redact(text)
    return redacted if redacted == text or _valid_json(redacted) else None


def dumps(model: BaseModel | dict | Any) -> str:
    data = redact_data(_jsonable(model))
    redacted = _text_redaction_keeping_valid_json(_serialise(data))
    if redacted is not None:
        return redacted
    text = _serialise(_redact_numeric_secrets(data))
    redacted = _text_redaction_keeping_valid_json(text)
    if redacted is not None:
        return redacted
    log.warning("text-level redaction would corrupt the JSON document; keeping per-value redaction")
    return text


def _write_synced(fd: int, text: str) -> None:
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def _discard_temp_file_keeping_original_error(tmp: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(tmp)


def _persist_rename_best_effort(directory: Path) -> None:
    with contextlib.suppress(OSError):
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def atomic_write_text(path: str | Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    replaced = False
    try:
        _write_synced(fd, utf8_safe(text))
        os.chmod(tmp, REPORT_FILE_MODE)
        os.replace(tmp, path)
        replaced = True
    finally:
        if not replaced:
            _discard_temp_file_keeping_original_error(tmp)
    _persist_rename_best_effort(path.parent)
    return path


def write_json(model: BaseModel | dict, path: str | Path) -> Path:
    return atomic_write_text(path, dumps(model))


def _validation_summary(exc: ValidationError) -> str:
    errs = exc.errors()
    shown = "; ".join(
        f"{'.'.join(str(x) for x in e.get('loc', ())) or '<root>'}: {e.get('msg', '')}"
        for e in errs[:MAX_VALIDATION_ERRORS_SHOWN]
    )
    hidden = len(errs) - MAX_VALIDATION_ERRORS_SHOWN
    return shown + (f" (+{hidden} more)" if hidden > 0 else "")


def _read_model(path: str | Path, cls: type[_M], default_name: str, what: str) -> _M:
    p = Path(path)
    if p.is_dir():
        p = p / default_name
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise LeanCError(f"{what} not found: {p}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise LeanCError(f"cannot read {what} {p}: {exc}") from None
    try:
        return cls.model_validate_json(text)
    except ValidationError as exc:
        raise LeanCError(f"{p} is not a valid {what}: {_validation_summary(exc)}") from None


def read_run_record(path: str | Path) -> RunRecord:
    return _read_model(path, RunRecord, "run.json", "run record")


def read_verification(path: str | Path) -> VerificationResult:
    return _read_model(path, VerificationResult, "verification.json", "verification result")


def _failure_entry(stage: str, f: FailureReport) -> dict[str, Any]:
    return {
        "id": f.id,
        "kind": f.kind,
        "tool": f.tool,
        "stage": stage,
        "property_id": f.property_id,
        "description": f.description,
        "location": f.location.model_dump(mode="json") if f.location else None,
        "inputs": [i.model_dump(mode="json") for i in f.inputs],
        "raw_ref": f.raw_ref,
        "ownership_trace": f.ownership_trace.model_dump(mode="json") if f.ownership_trace else None,
    }


def _approval_entry(a: ApprovalCheck | None) -> dict[str, Any]:
    if a is None:
        return {
            "approved": False,
            "approved_by": None,
            "approved_at": None,
            "signature_valid": False,
            "reasons": ["no approval check was recorded for this verification"],
        }
    return {
        "approved": a.approved,
        "approved_by": a.approved_by,
        "approved_at": a.approved_at,
        "signature_valid": a.signature_valid,
        "reasons": list(a.reasons),
    }


def summary(vr: VerificationResult) -> dict[str, Any]:
    approval = _approval_entry(vr.approval)
    stages = [
        {
            "stage": s.stage,
            "outcome": s.outcome.value,
            "status": s.status.value if s.status else None,
            "required": s.required,
            "summary": s.summary,
            "tools": detecting_tools(s),
            "invoked": invoked_tools(s),
            "failures": len(s.failures),
            "duration_s": s.duration_s,
        }
        for s in vr.stages
    ]
    failures = [_failure_entry(s.stage, f) for s in vr.stages for f in s.failures]
    data: dict[str, Any] = {
        "run_id": vr.run_id,
        "status": vr.status.value,
        "exit_code": exit_code(vr.status),
        "success": is_success(vr.status),
        "reasons": list(vr.reasons),
        "spec_id": vr.spec_id,
        "spec_version": vr.spec_version,
        "spec_hash": vr.spec_hash,
        "approval": approval,
        "stages": stages,
        "failures": failures,
        "detected_by": sort_tools(f["tool"] for f in failures),
        "assumptions": list(vr.assumptions),
        "limitations": list(vr.limitations),
    }
    return redact_data(data)
