"""Spec schema, hashing, approval trust boundary and workspace guards (no external tools)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from leanc.foundation.errors import ApprovalError, SpecError, WorkspaceError
from leanc.foundation.workspace import Workspace, normalize_relpath, safe_rmtree
from leanc.specs.approval import approvals_dir, approve_spec, check_approval, leanc_home
from leanc.specs.loader import load_spec, parse_spec_text, spec_hash
from leanc.specs.model import MANDATORY_CHECKS


@pytest.fixture
def project(example_copy) -> Path:
    return example_copy("safe_array_get")


# --------------------------------------------------------------- workspace
@pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "a/../../b", "a/..", "", "C:\\x", "a\\..\\b", "x\x00y"])
def test_normalize_rejects_escapes(bad):
    with pytest.raises(WorkspaceError):
        normalize_relpath(bad)


def test_normalize_accepts_clean_paths():
    assert normalize_relpath("./src//a.c") == "src/a.c"


def test_workspace_rejects_symlink_components(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f.c").write_text("x")
    os.symlink(tmp_path / "real", tmp_path / "link")
    ws = Workspace(tmp_path)
    with pytest.raises(WorkspaceError):
        ws.read_text("link/f.c")
    outside = tmp_path.parent / "outside_target.txt"
    outside.write_text("secret")
    os.symlink(outside, tmp_path / "evil.c")
    with pytest.raises(WorkspaceError):
        ws.read_text("evil.c")
    with pytest.raises(WorkspaceError):
        ws.write_text("evil.c", "overwrite")
    assert outside.read_text() == "secret"


def test_workspace_write_is_atomic_and_contained(tmp_path):
    ws = Workspace(tmp_path)
    ws.write_text("a/b/c.c", "int x;\n")
    assert (tmp_path / "a/b/c.c").read_text() == "int x;\n"
    assert not list((tmp_path / "a/b").glob(".leanc-tmp-*"))
    with pytest.raises(WorkspaceError):
        ws.write_text("../escape.c", "x")


def test_safe_rmtree_refuses_outside(tmp_path):
    inner = tmp_path / "w" / "run"
    inner.mkdir(parents=True)
    with pytest.raises(WorkspaceError):
        safe_rmtree(tmp_path, tmp_path / "w")
    safe_rmtree(inner, tmp_path / "w")
    assert not inner.exists()


# --------------------------------------------------------------- spec schema
def test_example_spec_loads_with_mandatory_checks(project):
    spec = load_spec(project / "spec.yaml")
    eff = spec.verification.effective_checks()
    for c in MANDATORY_CHECKS:
        assert c in eff
    assert spec.protected_paths() == [
        "harness/safe_array_get_harness.c",
        "include/safe_array.h",
        "tests/test_safe_array_get.c",
    ]


def test_spec_without_checks_still_gets_mandatory_ones(project):
    text = (project / "spec.yaml").read_text().replace("checks: [unsigned-overflow, conversion]", "checks: []")
    spec = parse_spec_text(text)
    assert spec.verification.checks == []
    assert set(MANDATORY_CHECKS) <= set(spec.verification.effective_checks())


def test_unknown_field_is_rejected(project):
    text = (project / "spec.yaml").read_text() + "\nskip_verification: true\n"
    with pytest.raises(SpecError, match="skip_verification"):
        parse_spec_text(text)


def test_duplicate_keys_are_rejected(project):
    text = (project / "spec.yaml").read_text() + "\nversion: 2\n"
    with pytest.raises(SpecError, match="duplicate key"):
        parse_spec_text(text)


def test_harness_cannot_be_editable(project):
    text = (project / "spec.yaml").read_text().replace(
        "editable: [src/safe_array.c]", "editable: [src/safe_array.c, harness/safe_array_get_harness.c]"
    )
    with pytest.raises(SpecError, match="both editable and protected"):
        parse_spec_text(text)


def test_spec_paths_cannot_escape(project):
    text = (project / "spec.yaml").read_text().replace("editable: [src/safe_array.c]", "editable: [../outside.c]")
    with pytest.raises(SpecError, match="traversal"):
        parse_spec_text(text)


def test_unknown_check_name_is_rejected(project):
    text = (project / "spec.yaml").read_text().replace("checks: [unsigned-overflow, conversion]", "checks: [none]")
    with pytest.raises(SpecError):
        parse_spec_text(text)


def test_hash_ignores_comments_but_not_content(project):
    text = (project / "spec.yaml").read_text()
    h = spec_hash(parse_spec_text(text))
    assert spec_hash(parse_spec_text("# extra comment\n" + text)) == h
    assert spec_hash(parse_spec_text(text.replace("unwind: 9", "unwind: 8"))) != h


# --------------------------------------------------------------- approval
def test_unapproved_by_default(project):
    spec = load_spec(project / "spec.yaml")
    chk = check_approval(spec, Workspace(project))
    assert not chk.approved
    assert "no approval record" in chk.reasons[0]


def test_approve_then_check(project, _isolated_leanc_home):
    ws = Workspace(project)
    spec = load_spec(project / "spec.yaml")
    rec = approve_spec(spec, "spec.yaml", ws, approved_by="alice")
    assert rec["approved_by"] == "alice"
    chk = check_approval(spec, ws)
    assert chk.approved and chk.signature_valid and chk.reasons == []
    key = leanc_home() / "approval.key"
    assert key.exists() and oct(key.stat().st_mode & 0o777) == "0o600"
    assert not str(key).startswith(str(project))


@pytest.mark.parametrize(
    "rel",
    ["harness/safe_array_get_harness.c", "tests/test_safe_array_get.c", "include/safe_array.h"],
)
def test_modifying_a_protected_file_revokes_approval(project, rel):
    ws = Workspace(project)
    spec = load_spec(project / "spec.yaml")
    approve_spec(spec, "spec.yaml", ws, approved_by="alice")
    p = project / rel
    p.write_text(p.read_text() + "\n/* weakened */\n")
    chk = check_approval(spec, ws)
    assert not chk.approved
    assert any(rel in r and "modified" in r for r in chk.reasons)


def test_modifying_the_spec_revokes_approval(project):
    ws = Workspace(project)
    spec = load_spec(project / "spec.yaml")
    approve_spec(spec, "spec.yaml", ws, approved_by="alice")
    weaker = parse_spec_text((project / "spec.yaml").read_text().replace("unwind: 9", "unwind: 2"))
    chk = check_approval(weaker, ws)
    assert not chk.approved
    assert any("hash mismatch" in r for r in chk.reasons)


def test_forged_approval_record_is_detected(project):
    ws = Workspace(project)
    spec = load_spec(project / "spec.yaml")
    approve_spec(spec, "spec.yaml", ws, approved_by="alice")
    # An attacker (e.g. a patch) edits the spec AND rewrites the record's hash.
    weaker = parse_spec_text((project / "spec.yaml").read_text().replace("unwind: 9", "unwind: 2"))
    rec_path = approvals_dir(project) / "safe-array-get.json"
    store = json.loads(rec_path.read_text())
    store["current"]["spec_hash"] = spec_hash(weaker)
    rec_path.write_text(json.dumps(store))
    chk = check_approval(weaker, ws)
    assert not chk.approved
    assert any("signature is invalid" in r for r in chk.reasons)


def test_missing_or_different_key_means_unapproved(project, monkeypatch, tmp_path):
    ws = Workspace(project)
    spec = load_spec(project / "spec.yaml")
    approve_spec(spec, "spec.yaml", ws, approved_by="alice")
    monkeypatch.setenv("LEANC_HOME", str(tmp_path / "other_home"))
    chk = check_approval(spec, ws)
    assert not chk.approved and any("key not found" in r for r in chk.reasons)
    approve_spec_other = tmp_path / "other_home"
    approve_spec_other.mkdir()
    (approve_spec_other / "approval.key").write_text("ab" * 32)
    chk = check_approval(spec, ws)
    assert not chk.approved and any("different key" in r for r in chk.reasons)


def test_changed_content_requires_version_bump(project):
    ws = Workspace(project)
    spec = load_spec(project / "spec.yaml")
    approve_spec(spec, "spec.yaml", ws, approved_by="alice")
    approve_spec(spec, "spec.yaml", ws, approved_by="alice")  # idempotent re-approval is fine
    changed = parse_spec_text((project / "spec.yaml").read_text().replace("unwind: 9", "unwind: 12"))
    with pytest.raises(ApprovalError, match="bump"):
        approve_spec(changed, "spec.yaml", ws, approved_by="alice")
    bumped = parse_spec_text(
        (project / "spec.yaml").read_text().replace("unwind: 9", "unwind: 12").replace("version: 1", "version: 2")
    )
    approve_spec(bumped, "spec.yaml", ws, approved_by="bob")
    assert check_approval(bumped, ws).approved
    assert not check_approval(spec, ws).approved  # the old version is no longer current
    store = json.loads((approvals_dir(project) / "safe-array-get.json").read_text())
    assert len(store["history"]) == 2


def test_approver_name_required(project):
    spec = load_spec(project / "spec.yaml")
    with pytest.raises(ApprovalError):
        approve_spec(spec, "spec.yaml", Workspace(project), approved_by="  ")


def test_rollback_to_superseded_version_is_rejected(project):
    ws = Workspace(project)
    v1_text = (project / "spec.yaml").read_text()
    v1 = parse_spec_text(v1_text)
    approve_spec(v1, "spec.yaml", ws, approved_by="alice")
    v1_store = (approvals_dir(project) / "safe-array-get.json").read_text()
    v2 = parse_spec_text(v1_text.replace("unwind: 9", "unwind: 12").replace("version: 1", "version: 2"))
    approve_spec(v2, "spec.yaml", ws, approved_by="alice")
    # Restore the old (signed, genuine) v1 record and the v1 spec.
    (approvals_dir(project) / "safe-array-get.json").write_text(v1_store)
    chk = check_approval(v1, ws)
    assert not chk.approved
    assert any("rollback" in r for r in chk.reasons)


def test_concurrent_key_creation_yields_one_valid_key(tmp_path, monkeypatch):
    import multiprocessing as mp

    monkeypatch.setenv("LEANC_HOME", str(tmp_path / "home"))
    ctx = mp.get_context("fork")
    with ctx.Pool(8) as pool:
        keys = pool.map(_create_key, range(8))
    assert len(set(keys)) == 1 and len(bytes.fromhex(keys[0])) == 32


def _create_key(_):
    from leanc.specs.approval import load_key

    return load_key(create=True).hex()


def test_duplicate_editable_paths_rejected(project):
    text = (project / "spec.yaml").read_text().replace(
        "editable: [src/safe_array.c]", "editable: [src/safe_array.c, ./src/safe_array.c]"
    )
    with pytest.raises(SpecError, match="duplicate"):
        parse_spec_text(text)


def test_non_utf8_source_is_a_readable_error(tmp_path):
    (tmp_path / "a.c").write_bytes(b"/* J\xfcrgen */\n")
    with pytest.raises(WorkspaceError, match="UTF-8"):
        Workspace(tmp_path).read_text("a.c")


def test_write_preserves_mode(tmp_path):
    p = tmp_path / "a.c"
    p.write_text("x")
    os.chmod(p, 0o600)
    Workspace(tmp_path).write_text("a.c", "y")
    assert p.read_text() == "y" and (p.stat().st_mode & 0o777) == 0o600


def test_approving_a_superseded_version_is_refused(project):
    ws = Workspace(project)
    text = (project / "spec.yaml").read_text()
    approve_spec(parse_spec_text(text), "spec.yaml", ws, approved_by="a")
    approve_spec(parse_spec_text(text.replace("unwind: 9", "unwind: 12").replace("version: 1", "version: 2")),
                 "spec.yaml", ws, approved_by="a")
    (approvals_dir(project) / "safe-array-get.json").unlink()  # even with the record store reset
    with pytest.raises(ApprovalError, match="bump"):
        approve_spec(parse_spec_text(text), "spec.yaml", ws, approved_by="a")
    with pytest.raises(ApprovalError, match="bump"):
        approve_spec(parse_spec_text(text.replace("unwind: 9", "unwind: 3").replace("version: 1", "version: 2")),
                     "spec.yaml", ws, approved_by="a")


def test_tampered_ledger_is_never_rewritten(project, _isolated_leanc_home):
    ws = Workspace(project)
    spec = load_spec(project / "spec.yaml")
    approve_spec(spec, "spec.yaml", ws, approved_by="a")
    ledger = _isolated_leanc_home / "approval-ledger.json"
    data = json.loads(ledger.read_text())
    data["signature"] = "0" * 64
    ledger.write_text(json.dumps(data))
    assert not check_approval(spec, ws).approved
    with pytest.raises(ApprovalError, match="ledger"):
        approve_spec(spec, "spec.yaml", ws, approved_by="a")
    assert json.loads(ledger.read_text())["signature"] == "0" * 64
