from __future__ import annotations

import fcntl
import hmac
import json
import os
import secrets
import tempfile
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any, NamedTuple

from leanc import __version__
from leanc.foundation.errors import JSON_LOAD_ERRORS, ApprovalError, WorkspaceError
from leanc.foundation.hashing import canonical_json, sha256_bytes
from leanc.foundation.models import ApprovalCheck, utcnow
from leanc.foundation.text import has_lone_surrogate
from leanc.foundation.workspace import Workspace
from leanc.specs.loader import spec_canonical_dict, spec_hash
from leanc.specs.model import Spec

KEY_FILE = "approval.key"
LEDGER_FILE = "approval-ledger.json"
LEDGER_LOCK_FILE = ".approval-ledger.lock"
KEY_TEMP_PREFIX = ".approval-key-"
LEDGER_TEMP_PREFIX = ".ledger-"
KEY_BYTES = 32
MIN_KEY_BYTES = 16
KEY_ID_HEX_CHARS = 16
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
LEDGER_CORRUPT = "approval ledger is corrupt"


class _LedgerRead(NamedTuple):
    entries: dict[str, Any]
    problem: str | None


class _LedgerFloor(NamedTuple):
    version: int
    spec_hash: str | None


class _StoreRead(NamedTuple):
    store: dict[str, Any] | None
    problem: str | None


class _Authentication(NamedTuple):
    signature_valid: bool
    reasons: list[str]


def leanc_home() -> Path:
    env = os.environ.get("LEANC_HOME")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "leanc"


def _key_path() -> Path:
    return leanc_home() / KEY_FILE


def _read_key(p: Path) -> bytes:
    try:
        key = bytes.fromhex(p.read_text(encoding="ascii").strip())
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApprovalError(f"corrupt approval key {p}") from exc
    if len(key) < MIN_KEY_BYTES:
        raise ApprovalError(f"corrupt approval key {p} (too short)")
    return key


def load_key(create: bool = False) -> bytes | None:
    p = _key_path()
    if p.is_symlink():
        raise ApprovalError(f"approval key must not be a symbolic link: {p}")
    if p.is_file():
        return _read_key(p)
    if not create:
        return None
    return _create_key(p)


def _create_key(p: Path) -> bytes:
    p.parent.mkdir(parents=True, exist_ok=True)
    _restrict_to_owner_if_owned(p.parent)
    fd, tmp = tempfile.mkstemp(prefix=KEY_TEMP_PREFIX, dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(secrets.token_bytes(KEY_BYTES).hex() + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, PRIVATE_FILE_MODE)
        _link_complete_key_unless_another_creator_won(tmp, p)
    finally:
        _remove_if_present(tmp)
    return _read_key(p)


def _restrict_to_owner_if_owned(directory: Path) -> None:
    if directory.stat().st_uid == os.geteuid():
        os.chmod(directory, PRIVATE_DIR_MODE)


def _link_complete_key_unless_another_creator_won(complete_key: str, p: Path) -> None:
    try:
        os.link(complete_key, p)
    except FileExistsError:
        pass


def _remove_if_present(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def key_id(key: bytes) -> str:
    return sha256(key).hexdigest()[:KEY_ID_HEX_CHARS]


def _sign(record: dict[str, Any], key: bytes) -> str:
    body = {k: v for k, v in record.items() if k != "signature"}
    return hmac.new(key, canonical_json(body).encode("utf-8"), sha256).hexdigest()


def _ledger_signature(entries: Any, key: bytes) -> str:
    return hmac.new(key, canonical_json(entries).encode("utf-8"), sha256).hexdigest()


def _ledger_key(root: Path, spec_id: str) -> str:
    return f"{os.path.realpath(root)}::{spec_id}"


def _load_ledger(key: bytes) -> _LedgerRead:
    p = leanc_home() / LEDGER_FILE
    if not p.is_file() or p.is_symlink():
        return _LedgerRead({}, None)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, *JSON_LOAD_ERRORS):
        return _LedgerRead({}, LEDGER_CORRUPT)
    if not isinstance(data, dict):
        return _LedgerRead({}, LEDGER_CORRUPT)
    entries = data.get("entries", {})
    if not hmac.compare_digest(str(data.get("signature", "")), _ledger_signature(entries, key)):
        return _LedgerRead({}, "approval ledger signature is invalid")
    return _LedgerRead(entries, None)


@contextmanager
def _cross_process_ledger_lock():
    home = leanc_home()
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / LEDGER_LOCK_FILE, os.O_RDWR | os.O_CREAT, PRIVATE_FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _ledger_floor(key: bytes, root: Path, spec_id: str) -> _LedgerFloor:
    entries, problem = _load_ledger(key)
    if problem:
        raise ApprovalError(
            f"{problem}; refusing to approve (restore {leanc_home() / LEDGER_FILE} or remove it deliberately)"
        )
    current = entries.get(_ledger_key(root, spec_id))
    if not current:
        return _LedgerFloor(0, None)
    return _LedgerFloor(int(current.get("version", 0)), current.get("spec_hash"))


def _update_ledger(key: bytes, root: Path, spec_id: str, version: int, spec_hash_: str) -> None:
    entries, problem = _load_ledger(key)
    if problem:
        raise ApprovalError(f"{problem}; refusing to rewrite it")
    ledger_key = _ledger_key(root, spec_id)
    current = entries.get(ledger_key)
    if current is None or int(current.get("version", 0)) <= version:
        entries[ledger_key] = {"version": version, "spec_hash": spec_hash_}
    _write_ledger(entries, key)


def _write_ledger(entries: dict[str, Any], key: bytes) -> None:
    body = {"entries": entries, "signature": _ledger_signature(entries, key)}
    p = leanc_home() / LEDGER_FILE
    fd, tmp = tempfile.mkstemp(prefix=LEDGER_TEMP_PREFIX, dir=p.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=2)
    os.chmod(tmp, PRIVATE_FILE_MODE)
    os.replace(tmp, p)


def approvals_dir(root: Path) -> Path:
    return Path(root) / ".leanc" / "approvals"


def _record_path(root: Path, spec_id: str) -> Path:
    return approvals_dir(root) / f"{spec_id}.json"


def protected_file_hashes(spec: Spec, ws: Workspace) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in spec.protected_paths():
        out[rel] = sha256_bytes(ws.read_bytes(rel))
    return out


def _read_store(root: Path, spec_id: str) -> _StoreRead:
    p = _record_path(root, spec_id)
    if not p.exists():
        return _StoreRead(None, None)
    if p.is_symlink():
        return _StoreRead(None, f"approval record must not be a symlink: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, *JSON_LOAD_ERRORS) as exc:
        return _StoreRead(None, f"corrupt approval record {p}: {exc}")
    if not isinstance(data, dict) or "current" not in data:
        return _StoreRead(None, f"corrupt approval record {p}")
    return _StoreRead(data, None)


def load_store(root: Path, spec_id: str) -> dict[str, Any] | None:
    store, problem = _read_store(root, spec_id)
    if problem:
        raise ApprovalError(problem)
    return store


def _reject_version_below_ledger_floor(key: bytes, root: Path, spec: Spec, content_hash: str) -> None:
    with _cross_process_ledger_lock():
        floor = _ledger_floor(key, root, spec.spec_id)
    same_version_other_content = spec.version == floor.version and floor.spec_hash not in (None, content_hash)
    if spec.version < floor.version or same_version_other_content:
        raise ApprovalError(
            f"version {spec.version} of '{spec.spec_id}' conflicts with approved version {floor.version}; "
            f"bump 'version' to at least {floor.version + 1} to approve this content"
        )


def _reject_changed_content_without_version_bump(
    current: dict[str, Any], spec: Spec, content_hash: str, files: dict[str, str]
) -> None:
    same_content = current.get("spec_hash") == content_hash and current.get("protected_files") == files
    if not same_content and spec.version <= int(current.get("spec_version", 0)):
        raise ApprovalError(
            f"spec content changed since version {current.get('spec_version')} was approved; "
            f"bump 'version' (currently {spec.version}) before approving the new content"
        )


def _write_store(ws: Workspace, spec_id: str, store: dict[str, Any]) -> None:
    directory = approvals_dir(ws.root)
    for part in (ws.root / ".leanc", directory):
        if part.is_symlink():
            raise ApprovalError(f"{part} must not be a symbolic link")
    directory.mkdir(parents=True, exist_ok=True)
    ws.write_text(
        ws.relpath(directory) + f"/{spec_id}.json",
        json.dumps(store, indent=2, ensure_ascii=False) + "\n",
    )


def approve_spec(
    spec: Spec,
    spec_rel_path: str,
    ws: Workspace,
    *,
    approved_by: str,
    note: str = "",
) -> dict[str, Any]:
    if not approved_by.strip():
        raise ApprovalError("approver name is required")
    if any(has_lone_surrogate(text) for text in (approved_by, note, spec_rel_path)):
        raise ApprovalError("approver name, note and spec path must be valid Unicode text (not raw non-UTF-8 bytes)")
    if spec_rel_path in spec.editable_paths():
        raise ApprovalError(
            f"the spec file {spec_rel_path} is also an editable file of the spec; repair could rewrite it"
        )
    key = load_key(create=True)
    assert key is not None
    content_hash = spec_hash(spec)
    files = protected_file_hashes(spec, ws)
    store = load_store(ws.root, spec.spec_id) or {"current": None, "history": []}
    current = store.get("current")
    _reject_version_below_ledger_floor(key, ws.root, spec, content_hash)
    if current:
        _reject_changed_content_without_version_bump(current, spec, content_hash, files)
    record: dict[str, Any] = {
        "spec_id": spec.spec_id,
        "spec_version": spec.version,
        "spec_hash": content_hash,
        "spec_path": spec_rel_path,
        "protected_files": files,
        "approved_by": approved_by.strip(),
        "approved_at": utcnow(),
        "origin": spec.provenance.origin,
        "note": note,
        "leanc_version": __version__,
        "key_id": key_id(key),
        "spec_snapshot": spec_canonical_dict(spec),
    }
    record["signature"] = _sign(record, key)
    if current:
        store["history"].append(current)
    store["current"] = record
    _write_store(ws, spec.spec_id, store)
    with _cross_process_ledger_lock():
        _update_ledger(key, ws.root, spec.spec_id, spec.version, content_hash)
    return record


def _authenticate(record: dict[str, Any], root: Path, spec_id: str) -> _Authentication:
    reasons: list[str] = []
    try:
        key = load_key(create=False)
    except ApprovalError as exc:
        key = None
        reasons.append(str(exc))
    if key is None:
        reasons.append(f"approval signing key not found at {_key_path()} (cannot authenticate record)")
        return _Authentication(False, reasons)
    if record.get("key_id") != key_id(key):
        reasons.append("approval record was signed with a different key")
        return _Authentication(False, reasons)
    signature_valid = hmac.compare_digest(str(record.get("signature", "")), _sign(record, key))
    if not signature_valid:
        reasons.append("approval record signature is invalid (record was modified)")
    reasons += _ledger_reasons(key, root, spec_id, record)
    return _Authentication(signature_valid, reasons)


def _ledger_reasons(key: bytes, root: Path, spec_id: str, record: dict[str, Any]) -> list[str]:
    entries, problem = _load_ledger(key)
    reasons = [problem] if problem else []
    latest = entries.get(_ledger_key(root, spec_id))
    if latest and int(latest.get("version", 0)) > int(record.get("spec_version", 0)):
        reasons.append(
            f"approval rollback: version {record.get('spec_version')} was superseded by approved version "
            f"{latest.get('version')}"
        )
    return reasons


def _content_mismatches(record: dict[str, Any], spec: Spec, content_hash: str) -> list[str]:
    reasons: list[str] = []
    if record.get("spec_id") != spec.spec_id:
        reasons.append("approval record is for a different spec id")
    if int(record.get("spec_version", -1)) != spec.version:
        reasons.append(f"spec version {spec.version} is not the approved version {record.get('spec_version')}")
    if record.get("spec_hash") != content_hash:
        reasons.append("spec content changed since approval (hash mismatch)")
    return reasons


def _protected_file_mismatches(spec: Spec, approved_files: dict[str, str], files_ws: Workspace) -> list[str]:
    reasons: list[str] = []
    current_paths = set(spec.protected_paths())
    if set(approved_files) != current_paths:
        reasons.append("set of protected files differs from the approved set")
    for rel in sorted(current_paths):
        try:
            content = files_ws.read_bytes(rel)
        except (WorkspaceError, OSError) as exc:
            reasons.append(f"protected file {rel} unreadable: {exc}")
            continue
        if approved_files.get(rel) != sha256_bytes(content):
            reasons.append(f"protected file {rel} was modified since approval (hash mismatch)")
    return reasons


def check_approval(spec: Spec, ws: Workspace, files_ws: Workspace | None = None) -> ApprovalCheck:
    files_ws = files_ws or ws
    content_hash = spec_hash(spec)
    identity = dict(spec_id=spec.spec_id, spec_version=spec.version, spec_hash=content_hash)
    store, problem = _read_store(ws.root, spec.spec_id)
    if problem:
        return ApprovalCheck(approved=False, reasons=[problem], **identity)
    if not store or not store.get("current"):
        return ApprovalCheck(
            approved=False,
            reasons=[f"no approval record for spec '{spec.spec_id}' (run `leanc spec approve`)"],
            **identity,
        )
    record = store["current"]
    signature_valid, reasons = _authenticate(record, ws.root, spec.spec_id)
    reasons += _content_mismatches(record, spec, content_hash)
    approved_files: dict[str, str] = dict(record.get("protected_files") or {})
    reasons += _protected_file_mismatches(spec, approved_files, files_ws)
    return ApprovalCheck(
        approved=not reasons,
        approved_by=record.get("approved_by"),
        approved_at=record.get("approved_at"),
        signature_valid=signature_valid,
        reasons=reasons,
        protected_files=approved_files,
        **identity,
    )


def approved_snapshot(root: Path, spec_id: str) -> dict[str, Any] | None:
    store = _read_store(root, spec_id).store
    if not store or not store.get("current"):
        return None
    return store["current"].get("spec_snapshot")
