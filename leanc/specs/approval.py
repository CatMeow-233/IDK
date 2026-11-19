"""Spec approval: the trust boundary between AI-proposed and trusted artefacts.

An *approval record* binds together

* the spec id and version,
* the semantic hash of the spec (:func:`leanc.specs.loader.spec_hash`),
* SHA-256 of every protected file (harness, test driver, interface headers),
* who approved it and when,

and is signed with HMAC-SHA256 using a per-user key stored **outside the
workspace** (``$LEANC_HOME/approval.key``, default ``~/.config/leanc``).  Code
under test, generated code and repair patches only ever touch editable sources
inside the workspace, so they can neither edit the protected files without
invalidating the hashes, nor forge a new approval record.

Before every verification the controller calls :func:`check_approval`; any
mismatch yields ``SPEC_UNAPPROVED`` and no verified status can be reported.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any

from leanc import __version__
from leanc.core.errors import ApprovalError
from leanc.core.hashing import canonical_json, sha256_bytes
from leanc.core.models import ApprovalCheck, utcnow
from leanc.core.workspace import Workspace
from leanc.specs.loader import spec_canonical_dict, spec_hash
from leanc.specs.model import Spec

KEY_FILE = "approval.key"


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
        data = p.read_text(encoding="ascii").strip()
        key = bytes.fromhex(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApprovalError(f"corrupt approval key {p}") from exc
    if len(key) < 16:
        raise ApprovalError(f"corrupt approval key {p} (too short)")
    return key


def load_key(create: bool = False) -> bytes | None:
    """Load (or atomically create) the per-user approval signing key.

    Creation writes a complete temp file and hard-links it into place, so a
    concurrent reader never sees a partially written key and concurrent
    creators agree on a single key."""
    p = _key_path()
    if p.is_symlink():
        raise ApprovalError(f"approval key must not be a symbolic link: {p}")
    if p.is_file():
        return _read_key(p)
    if not create:
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".approval-key-", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(secrets.token_bytes(32).hex() + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        try:
            os.link(tmp, p)
        except FileExistsError:
            pass  # another process won the race; use its key
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return _read_key(p)


def key_id(key: bytes) -> str:
    return sha256(key).hexdigest()[:16]


def _sign(record: dict[str, Any], key: bytes) -> str:
    body = {k: v for k, v in record.items() if k != "signature"}
    return hmac.new(key, canonical_json(body).encode("utf-8"), sha256).hexdigest()


LEDGER_FILE = "approval-ledger.json"


def _ledger_key(root: Path, spec_id: str) -> str:
    return f"{os.path.realpath(root)}::{spec_id}"


def _load_ledger(key: bytes) -> tuple[dict[str, Any], str | None]:
    """Signed per-user record of the highest approved version per project and
    spec, kept outside the workspace.  Prevents rolling an approval record back
    to a superseded (e.g. weaker) version from inside the workspace."""
    p = leanc_home() / LEDGER_FILE
    if not p.is_file() or p.is_symlink():
        return {}, None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        entries = data.get("entries", {})
        sig = data.get("signature", "")
    except (OSError, ValueError, AttributeError):
        return {}, "approval ledger is corrupt"
    expected = hmac.new(key, canonical_json(entries).encode("utf-8"), sha256).hexdigest()
    if not hmac.compare_digest(str(sig), expected):
        return {}, "approval ledger signature is invalid"
    return entries, None


def _update_ledger(key: bytes, root: Path, spec_id: str, version: int, spec_hash_: str) -> None:
    entries, _err = _load_ledger(key)
    k = _ledger_key(root, spec_id)
    cur = entries.get(k)
    if cur is None or int(cur.get("version", 0)) <= version:
        entries[k] = {"version": version, "spec_hash": spec_hash_}
    body = {
        "entries": entries,
        "signature": hmac.new(key, canonical_json(entries).encode("utf-8"), sha256).hexdigest(),
    }
    p = leanc_home() / LEDGER_FILE
    fd, tmp = tempfile.mkstemp(prefix=".ledger-", dir=p.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=2)
    os.chmod(tmp, 0o600)
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


def load_store(root: Path, spec_id: str) -> dict[str, Any] | None:
    p = _record_path(root, spec_id)
    if not p.exists():
        return None
    if p.is_symlink():
        raise ApprovalError(f"approval record must not be a symlink: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApprovalError(f"corrupt approval record {p}: {exc}") from exc
    if not isinstance(data, dict) or "current" not in data:
        raise ApprovalError(f"corrupt approval record {p}")
    return data


def approve_spec(
    spec: Spec,
    spec_rel_path: str,
    ws: Workspace,
    *,
    approved_by: str,
    note: str = "",
) -> dict[str, Any]:
    """Create (or idempotently refresh) a signed approval record.

    Version rule: approving content that differs from the currently approved
    content requires a strictly greater ``version``.
    """
    if not approved_by.strip():
        raise ApprovalError("approver name is required")
    key = load_key(create=True)
    assert key is not None
    h = spec_hash(spec)
    files = protected_file_hashes(spec, ws)
    store = load_store(ws.root, spec.spec_id) or {"current": None, "history": []}
    cur = store.get("current")
    if cur:
        same_content = cur.get("spec_hash") == h and cur.get("protected_files") == files
        if not same_content and spec.version <= int(cur.get("spec_version", 0)):
            raise ApprovalError(
                f"spec content changed since version {cur.get('spec_version')} was approved; "
                f"bump 'version' (currently {spec.version}) before approving the new content"
            )
    record: dict[str, Any] = {
        "spec_id": spec.spec_id,
        "spec_version": spec.version,
        "spec_hash": h,
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
    if cur:
        store["history"].append(cur)
    store["current"] = record
    d = approvals_dir(ws.root)
    for part in (ws.root / ".leanc", d):
        if part.is_symlink():
            raise ApprovalError(f"{part} must not be a symbolic link")
    d.mkdir(parents=True, exist_ok=True)
    ws.write_text(
        ws.relpath(d) + f"/{spec.spec_id}.json",
        json.dumps(store, indent=2, ensure_ascii=False) + "\n",
    )
    _update_ledger(key, ws.root, spec.spec_id, spec.version, h)
    return record


def check_approval(spec: Spec, ws: Workspace, files_ws: Workspace | None = None) -> ApprovalCheck:
    """Verify that ``spec`` and its protected files match a valid, signed
    approval record.  Never raises for ordinary "not approved" situations.

    ``files_ws`` (default ``ws``) is where protected files are read from; the
    controller passes the run snapshot so that exactly the bytes that get
    verified are the bytes that get hash-checked."""
    files_ws = files_ws or ws
    reasons: list[str] = []
    h = spec_hash(spec)
    base = dict(spec_id=spec.spec_id, spec_version=spec.version, spec_hash=h)
    try:
        store = load_store(ws.root, spec.spec_id)
    except ApprovalError as exc:
        return ApprovalCheck(approved=False, reasons=[str(exc)], **base)
    if not store or not store.get("current"):
        return ApprovalCheck(
            approved=False,
            reasons=[f"no approval record for spec '{spec.spec_id}' (run `leanc spec approve`)"],
            **base,
        )
    rec = store["current"]
    try:
        key = load_key(create=False)
    except ApprovalError as exc:
        key = None
        reasons.append(str(exc))
    sig_ok = False
    if key is None:
        reasons.append(f"approval signing key not found at {_key_path()} (cannot authenticate record)")
    elif rec.get("key_id") != key_id(key):
        reasons.append("approval record was signed with a different key")
    else:
        sig_ok = hmac.compare_digest(str(rec.get("signature", "")), _sign(rec, key))
        if not sig_ok:
            reasons.append("approval record signature is invalid (record was modified)")
        entries, ledger_err = _load_ledger(key)
        if ledger_err:
            reasons.append(ledger_err)
        latest = entries.get(_ledger_key(ws.root, spec.spec_id))
        if latest and int(latest.get("version", 0)) > int(rec.get("spec_version", 0)):
            reasons.append(
                f"approval rollback: version {rec.get('spec_version')} was superseded by approved version "
                f"{latest.get('version')}"
            )
    if rec.get("spec_id") != spec.spec_id:
        reasons.append("approval record is for a different spec id")
    if int(rec.get("spec_version", -1)) != spec.version:
        reasons.append(
            f"spec version {spec.version} is not the approved version {rec.get('spec_version')}"
        )
    if rec.get("spec_hash") != h:
        reasons.append("spec content changed since approval (hash mismatch)")
    approved_files: dict[str, str] = dict(rec.get("protected_files") or {})
    current_paths = set(spec.protected_paths())
    if set(approved_files) != current_paths:
        reasons.append("set of protected files differs from the approved set")
    for rel in sorted(current_paths):
        try:
            actual = sha256_bytes(files_ws.read_bytes(rel))
        except Exception as exc:  # missing file, symlink, traversal ...
            reasons.append(f"protected file {rel} unreadable: {exc}")
            continue
        if approved_files.get(rel) != actual:
            reasons.append(f"protected file {rel} was modified since approval (hash mismatch)")
    return ApprovalCheck(
        approved=not reasons,
        approved_by=rec.get("approved_by"),
        approved_at=rec.get("approved_at"),
        signature_valid=sig_ok,
        reasons=reasons,
        protected_files=approved_files,
        **base,
    )


def approved_snapshot(root: Path, spec_id: str) -> dict[str, Any] | None:
    try:
        store = load_store(root, spec_id)
    except ApprovalError:
        return None
    if not store or not store.get("current"):
        return None
    return store["current"].get("spec_snapshot")
