from __future__ import annotations

import json
from pathlib import Path

import pytest
from reports_support import HASH, _inv, _run_record, _vr

from leanc.foundation.errors import LeanCError
from leanc.foundation.models import FailureReport, StageResult
from leanc.foundation.status import EXIT_CODES, Outcome, Status
from leanc.reports.json_report import (
    TOOL_ORDER,
    detecting_tools,
    dumps,
    read_run_record,
    read_verification,
    summary,
    write_json,
)
from leanc.reports.markdown import render_verification_md


def test_write_json_roundtrip_run_record(tmp_path):
    rr = _run_record()
    p = write_json(rr, tmp_path / "sub" / "run.json")
    assert p == tmp_path / "sub" / "run.json"
    text = p.read_text(encoding="utf-8")
    assert text.endswith("\n") and text.startswith("{\n  ")
    back = read_run_record(p)
    assert back == rr
    assert read_run_record(tmp_path / "sub") == rr
    assert back.repair_rounds[0].policy.violations[1].line == 9
    assert back.verification.stages[-1].outcome is Outcome.PASS
    assert sorted(x.name for x in p.parent.iterdir()) == ["run.json"]


def test_write_json_roundtrip_verification_and_dict(tmp_path):
    vr = _vr(Status.COUNTEREXAMPLE_FOUND)
    write_json(vr, tmp_path / "verification.json")
    assert read_verification(tmp_path / "verification.json") == vr
    assert read_verification(tmp_path) == vr
    write_json({"a": Status.UNKNOWN, "p": Path("x/y"), "n": [1, 2.5, None], "s": {"b", "a"}}, tmp_path / "d.json")
    assert json.loads((tmp_path / "d.json").read_text()) == {"a": "UNKNOWN", "p": "x/y", "n": [1, 2.5, None], "s": ["a", "b"]}


def test_write_json_is_deterministic_and_utf8(tmp_path):
    vr = _vr(Status.UNKNOWN, reasons=["需求：安全陣列讀取"])
    assert dumps(vr) == dumps(vr.model_copy(deep=True))
    write_json(vr, tmp_path / "v.json")
    assert "需求：安全陣列讀取" in (tmp_path / "v.json").read_text(encoding="utf-8")


def test_write_json_replaces_atomically(tmp_path):
    p = tmp_path / "run.json"
    p.write_text("old")
    target = tmp_path / "elsewhere.txt"
    target.write_text("keep")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    write_json({"x": 1}, p)
    write_json({"y": 2}, link)
    assert json.loads(p.read_text()) == {"x": 1}
    assert target.read_text() == "keep"
    assert not link.is_symlink() and json.loads(link.read_text()) == {"y": 2}


def test_write_json_redacts_secrets(tmp_path, monkeypatch):
    secret = "s3cr3t-Value-With\"Quote\\Slash"
    monkeypatch.setenv("LEANC_API_KEY", secret)
    monkeypatch.setenv("MY_SERVICE_TOKEN", "tok_ABCDEFGH12345")
    vr = _vr(Status.TOOL_ERROR)
    vr.stages[-1].summary = f"cbmc failed; env LEANC_API_KEY={secret}"
    vr.stages[-1].details = {"note": "Authorization: Bearer abcdefghijklmnop", "tok": "tok_ABCDEFGH12345"}
    vr.stages[-1].invocations = [_inv("cbmc", ["cbmc", "--api_key=zzzzzzzzzzzz", "sk-" + "A" * 24], "05-cbmc")]
    vr.reasons.append("ghp_" + "x" * 30)
    p = write_json(vr, tmp_path / "verification.json")
    text = p.read_text(encoding="utf-8")
    for leaked in (secret, "s3cr3t-Value", "tok_ABCDEFGH12345", "abcdefghijklmnop", "zzzzzzzzzzzz", "A" * 24, "x" * 30):
        assert leaked not in text, leaked
    assert "[REDACTED]" in text
    back = read_verification(p)
    assert "[REDACTED]" in back.stages[-1].summary
    assert secret not in json.dumps(summary(vr))
    md = render_verification_md(vr)
    assert secret not in md and "tok_ABCDEFGH12345" not in md and "[REDACTED]" in md


def test_numeric_secret_keeps_json_valid(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_TOKEN", "987654321")
    p = write_json({"line": 987654321, "ok": 3, "msg": "token 987654321"}, tmp_path / "x.json")
    data = json.loads(p.read_text())
    assert data == {"line": "[REDACTED]", "ok": 3, "msg": "token [REDACTED]"}


def test_read_errors_are_readable(tmp_path):
    with pytest.raises(LeanCError, match="not found"):
        read_run_record(tmp_path / "missing.json")
    bad = tmp_path / "run.json"
    bad.write_text("{not json")
    with pytest.raises(LeanCError, match="not a valid run record"):
        read_run_record(bad)
    bad.write_text(json.dumps({"run_id": "x"}))
    with pytest.raises(LeanCError, match="not a valid run record"):
        read_run_record(bad)
    write_json(_vr(Status.UNKNOWN), tmp_path / "v.json")
    with pytest.raises(LeanCError, match="not a valid run record"):
        read_run_record(tmp_path / "v.json")


@pytest.mark.parametrize("status", list(Status))
def test_summary_every_status(status):
    vr = _vr(status)
    s = summary(vr)
    assert s["status"] == status.value
    assert s["exit_code"] == EXIT_CODES[status]
    assert s["success"] is (status in (Status.VERIFIED_BOUNDED, Status.PROVED))
    assert (s["spec_id"], s["spec_version"], s["spec_hash"]) == ("safe-array-get", 2, HASH)
    assert s["approval"]["approved"] is (status is not Status.SPEC_UNAPPROVED)
    assert isinstance(s["approval"]["reasons"], list)
    assert [x["stage"] for x in s["stages"]] == [x.stage for x in vr.stages]
    for x, st in zip(s["stages"], vr.stages):
        assert x["outcome"] == st.outcome.value
        assert x["status"] == (st.status.value if st.status else None)
        assert set(x) >= {"stage", "outcome", "status", "summary", "tools"}
    assert s["assumptions"] == vr.assumptions and s["limitations"] == vr.limitations
    json.dumps(s)


def test_summary_failures_and_tools():
    s = summary(_vr(Status.RUNTIME_TEST_FAILED))
    tools = [f["tool"] for f in s["failures"]]
    assert tools == ["asan", "ubsan", "test-oracle"]
    asan = s["failures"][0]
    assert asan["kind"] == "runtime" and asan["stage"] == "sanitizer"
    assert asan["property_id"] == "heap-buffer-overflow"
    assert asan["location"]["file"] == "src/safe_array_get.c" and asan["location"]["line"] == 12
    assert asan["inputs"] == [{"name": "argv", "value": "get 3 7", "type": None}]
    san = next(x for x in s["stages"] if x["stage"] == "sanitizer")
    assert san["tools"] == ["asan", "ubsan", "test-oracle"]
    comp = next(x for x in s["stages"] if x["stage"] == "compile")
    assert comp["tools"] == ["clang"] and comp["invoked"] == ["clang"]
    assert s["detected_by"] == ["asan", "ubsan", "test-oracle"]
    ce = summary(_vr(Status.COUNTEREXAMPLE_FOUND))["failures"][0]
    assert ce["tool"] == "cbmc" and ce["inputs"][0] == {"name": "idx", "value": "4294967295u", "type": "unsigned int"}


def test_summary_without_approval_is_not_approved():
    s = summary(_vr(Status.UNKNOWN, approval=None))
    assert s["approval"]["approved"] is False and s["approval"]["reasons"]


def test_detecting_tools_order():
    st = StageResult(
        stage="x",
        outcome=Outcome.FAIL,
        failures=[FailureReport(id=t, kind="tool", tool=t, description="d") for t in ("zz-custom", "cbmc", "asan", "clang")],
    )
    assert detecting_tools(st) == ["clang", "asan", "cbmc", "zz-custom"]
    assert TOOL_ORDER.index("clang") < TOOL_ORDER.index("cbmc")
