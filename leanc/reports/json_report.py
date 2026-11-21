"""Machine-readable reports: ``run.json`` / ``verification.json`` and the
compact verdict summary embedded in ``leanc ... --json`` output.

Why this module is careful about three things:

* **Redaction.**  Reports embed tool output, argv, LLM-supplied rationale and
  file contents, any of which can contain a credential that leaked into the
  environment.  Every string is passed through :func:`leanc.foundation.redact.redact`
  before serialisation *and* the serialised text is redacted again, so secrets
  never reach disk whether they sit inside one JSON string or not.
* **Atomicity.**  ``run.json`` is the record ``leanc report`` re-reads; a crash
  mid-write must never leave a truncated file that later parses as something
  else.  We write to a temporary file in the same directory, ``fsync`` and
  ``os.replace`` it into place.
* **Honest summaries.**  :func:`summary` reports exactly what the
  :class:`VerificationResult` says (status, exit code, per-gate outcomes,
  failures with the *detecting* tool, assumptions and limitations); it never
  derives a more favourable verdict.
"""

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
from leanc.foundation.models import Diagnostic, FailureReport, RunRecord, StageResult, VerificationResult
from leanc.foundation.redact import REDACTED, redact
from leanc.foundation.status import exit_code, is_success

log = logging.getLogger("leanc.reports")

#: Canonical presentation order of detecting tools (compiler first, formal last).
#: Tools not listed here (e.g. ``leanc``, ``leanc-policy``) sort after these,
#: alphabetically.
TOOL_ORDER: tuple[str, ...] = (
    "clang",
    "clang-static-analyzer",
    "asan",
    "ubsan",
    "lsan",
    "test-oracle",
    "cbmc",
)

#: Diagnostic severities that count as a *finding* (notes / info only add
#: context to another diagnostic and do not name a detecting tool).
FINDING_SEVERITIES = frozenset({"error", "warning"})

_M = TypeVar("_M", bound=BaseModel)


# --------------------------------------------------------------------------- helpers
def tool_sort_key(tool: str) -> tuple[int, str]:
    """Sort key placing known tools in :data:`TOOL_ORDER`, others after them."""
    try:
        return (TOOL_ORDER.index(tool), tool)
    except ValueError:
        return (len(TOOL_ORDER), tool)


def sort_tools(tools: Iterable[str]) -> list[str]:
    return sorted({t for t in tools if t}, key=tool_sort_key)


def is_finding(d: Diagnostic) -> bool:
    return d.severity in FINDING_SEVERITIES


def detecting_tools(stage: StageResult) -> list[str]:
    """Tools that *flagged something* in this stage: the tool of every failure
    and of every error/warning diagnostic, in :data:`TOOL_ORDER`."""
    return sort_tools([f.tool for f in stage.failures] + [d.tool for d in stage.diagnostics if is_finding(d)])


def invoked_tools(stage: StageResult) -> list[str]:
    """Tools that were run in this stage (first-invocation order, deduplicated)."""
    seen: dict[str, None] = {}
    for inv in stage.invocations:
        seen.setdefault(inv.tool, None)
    return list(seen)


def _jsonable(obj: Any) -> Any:
    """Convert ``obj`` into plain JSON data (deterministic, no lossy ``str()``
    of unknown objects except as a last resort)."""
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
    if isinstance(obj, float) and not math.isfinite(obj):
        return str(obj)  # NaN / inf are not JSON; keep them visible instead of crashing the report
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


def redact_data(obj: Any) -> Any:
    """Recursively redact every string (keys included) of plain JSON data."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {redact(k) if isinstance(k, str) else k: redact_data(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_data(v) for v in obj]
    return obj


def _redact_numbers(obj: Any) -> Any:
    """Replace numbers whose JSON text is (part of) a secret by the redaction
    marker.  Only reached when a secret value happens to be numeric."""
    if isinstance(obj, dict):
        return {k: _redact_numbers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_numbers(v) for v in obj]
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


def dumps(model: BaseModel | dict | Any) -> str:
    """Serialise ``model`` as pretty, redacted UTF-8 JSON text (with a trailing
    newline).  Field order is the model's declaration order, so the output is
    deterministic for equal inputs."""
    data = redact_data(_jsonable(model))
    text = _serialise(data)
    red = redact(text)
    if red == text or _valid_json(red):
        return red
    # A secret matched outside any JSON string (a numeric secret value).  Redact
    # the offending numbers as data so the document stays valid JSON.
    data = _redact_numbers(data)
    text = _serialise(data)
    red = redact(text)
    if red == text or _valid_json(red):
        return red
    log.warning("text-level redaction would corrupt the JSON document; keeping per-value redaction")
    return text


def atomic_write_text(path: str | Path, text: str) -> Path:
    """Write ``text`` to ``path`` atomically (temp file in the same directory,
    fsync, ``os.replace``).  A pre-existing symlink at ``path`` is replaced, not
    followed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):  # best effort: persist the rename itself
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    return path


# --------------------------------------------------------------------------- public API
def write_json(model: BaseModel | dict, path: str | Path) -> Path:
    """Write ``model`` (a pydantic model or plain dict) to ``path`` as pretty,
    redacted UTF-8 JSON, atomically.  Returns the path written."""
    return atomic_write_text(path, dumps(model))


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
        errs = exc.errors()
        shown = "; ".join(
            f"{'.'.join(str(x) for x in e.get('loc', ())) or '<root>'}: {e.get('msg', '')}" for e in errs[:3]
        )
        more = f" (+{len(errs) - 3} more)" if len(errs) > 3 else ""
        raise LeanCError(f"{p} is not a valid {what}: {shown}{more}") from None


def read_run_record(path: str | Path) -> RunRecord:
    """Load and validate a ``run.json`` (``path`` may also be the run directory).
    Raises :class:`LeanCError` with a readable message when it is missing or invalid."""
    return _read_model(path, RunRecord, "run.json", "run record")


def read_verification(path: str | Path) -> VerificationResult:
    """Load and validate a ``verification.json`` (``path`` may also be its directory)."""
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
    }


def summary(vr: VerificationResult) -> dict[str, Any]:
    """Compact, machine-readable verdict of one verification.

    Keys: ``run_id``, ``status``, ``exit_code``, ``success``, ``reasons``,
    ``spec_id``/``spec_version``/``spec_hash``, ``approval`` (always a dict with
    ``approved`` and ``reasons``), ``stages`` (``stage``, ``outcome``,
    ``status``, ``required``, ``summary``, ``tools`` = detecting tools,
    ``invoked`` = tools run, ``failures`` = count, ``duration_s``),
    ``failures`` (``id``, ``kind``, ``tool``, ``stage``, ``property_id``,
    ``description``, ``location``, ``inputs``, ``raw_ref``), ``detected_by``,
    ``assumptions`` and ``limitations``.  All strings are redacted.
    """
    a = vr.approval
    if a is not None:
        approval: dict[str, Any] = {
            "approved": a.approved,
            "approved_by": a.approved_by,
            "approved_at": a.approved_at,
            "signature_valid": a.signature_valid,
            "reasons": list(a.reasons),
        }
    else:
        approval = {
            "approved": False,
            "approved_by": None,
            "approved_at": None,
            "signature_valid": False,
            "reasons": ["no approval check was recorded for this verification"],
        }
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
