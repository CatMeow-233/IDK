"""Tests for the JSON / Markdown report generators (synthetic results only:
no external tools are needed)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from leanc.core.errors import LeanCError
from leanc.core.models import (
    ApprovalCheck,
    CounterexampleInput,
    Diagnostic,
    EnvironmentInfo,
    FailureReport,
    GenerationRecord,
    Location,
    PolicyDecision,
    PolicyViolation,
    ProviderInfo,
    RepairRound,
    RunRecord,
    StageResult,
    ToolInfo,
    ToolInvocation,
    TraceStep,
    VerificationResult,
)
from leanc.core.status import EXIT_CODES, Outcome, Status, outcome_to_status
from leanc.reports.json_report import (
    TOOL_ORDER,
    detecting_tools,
    dumps,
    read_run_record,
    read_verification,
    summary,
    write_json,
)
from leanc.reports.markdown import (
    ALWAYS_LIMITATIONS,
    MOCK_DISCLAIMER,
    STATUS_MEANING,
    render_run_md,
    render_verification_md,
)

HASH = "ab" * 32


# --------------------------------------------------------------------------- builders
def _approval(approved: bool = True) -> ApprovalCheck:
    if approved:
        return ApprovalCheck(
            approved=True,
            spec_id="safe-array-get",
            spec_version=2,
            spec_hash=HASH,
            approved_by="alice",
            approved_at="2026-11-01T10:00:00+00:00",
            signature_valid=True,
            protected_files={"harness/h.c": "cd" * 32, "include/safe_array_get.h": "ef" * 32},
        )
    return ApprovalCheck(
        approved=False,
        spec_id="safe-array-get",
        spec_version=2,
        spec_hash=HASH,
        signature_valid=True,
        reasons=["protected file harness/h.c was modified since approval (hash mismatch)"],
    )


def _env(sandbox: str = "bwrap") -> EnvironmentInfo:
    return EnvironmentInfo(
        leanc_version="0.1.0",
        python="3.13.0",
        platform="Linux 6.1 x86_64",
        tools=[
            ToolInfo(name="clang", path="/usr/bin/clang", version="Ubuntu clang version 18.1.3", available=True),
            ToolInfo(name="cbmc", path="/usr/bin/cbmc", version="5.95.1", available=True),
            ToolInfo(
                name="bwrap",
                path="/usr/bin/bwrap" if sandbox == "bwrap" else None,
                available=sandbox == "bwrap",
                note=None if sandbox == "bwrap" else "bwrap not found",
            ),
        ],
        sandbox=sandbox,
        sandbox_note="bubblewrap: no network" if sandbox == "bwrap" else "bwrap not found",
    )


def _inv(tool: str, argv: list[str], log: str) -> ToolInvocation:
    return ToolInvocation(
        tool=tool,
        argv=argv,
        cwd="/runs/r1/snapshot",
        exit_code=0,
        duration_s=0.25,
        sandbox="none",
        stdout_path=f"logs/{log}.stdout.txt",
        stderr_path=f"logs/{log}.stderr.txt",
    )


def _stage(stage: str, outcome: Outcome, summary_text: str = "", **kw) -> StageResult:
    return StageResult(
        stage=stage,
        outcome=outcome,
        status=outcome_to_status(stage, outcome),
        summary=summary_text or f"{stage} {outcome.value.lower()}",
        duration_s=kw.pop("duration_s", 0.5),
        **kw,
    )


def _cbmc_failure() -> FailureReport:
    return FailureReport(
        id="cbmc:safe_array_get.pointer_dereference.7",
        kind="counterexample",
        tool="cbmc",
        property_id="safe_array_get.pointer_dereference.7",
        description="dereference failure: pointer outside object bounds in arr[(signed long int)idx]",
        location=Location(file="src/safe_array_get.c", line=12, column=9, function="safe_array_get"),
        inputs=[
            CounterexampleInput(name="idx", value="4294967295u", type="unsigned int"),
            CounterexampleInput(name="len", value="3", type="size_t"),
            CounterexampleInput(name="weird|name", value="a|b\nc", type=None),
        ],
        call_stack=["main", "safe_array_get"],
        trace=[
            TraceStep(kind="assignment", location=Location(file="harness/h.c", line=i), text=f"step_{i} = {i}")
            for i in range(1, 31)
        ]
        + [TraceStep(kind="failure", location=Location(file="src/safe_array_get.c", line=12), text="pointer outside object bounds")],
        explanation="idx is not checked against len before the access",
        raw_ref="raw/cbmc-verify.json",
    )


def _asan_failure() -> FailureReport:
    return FailureReport(
        id="asan:heap-buffer-overflow:src/safe_array_get.c:12",
        kind="runtime",
        tool="asan",
        property_id="heap-buffer-overflow",
        description="heap-buffer-overflow READ of size 4",
        location=Location(file="src/safe_array_get.c", line=12, column=9, function="safe_array_get"),
        inputs=[CounterexampleInput(name="argv", value="get 3 7")],
        call_stack=["safe_array_get src/safe_array_get.c:12", "main tests/test_driver.c:40"],
        raw_ref="raw/sanitizer-case-oob.txt",
    )


def _ubsan_failure() -> FailureReport:
    return FailureReport(
        id="ubsan:signed-integer-overflow:src/a.c:5",
        kind="runtime",
        tool="ubsan",
        property_id="signed-integer-overflow",
        description="signed integer overflow: 2147483647 + 1 cannot be represented in type 'int'",
        location=Location(file="src/a.c", line=5, column=14),
    )


def _static_failure() -> FailureReport:
    return FailureReport(
        id="static:core.NullDereference:src/a.c:3:4",
        kind="static",
        tool="clang-static-analyzer",
        property_id="core.NullDereference",
        description="Dereference of null pointer (loaded from variable 'p')",
        location=Location(file="src/a.c", line=3, column=4),
        trace=[TraceStep(kind="location", location=Location(file="src/a.c", line=2), text="'p' initialized to null")],
        raw_ref="raw/analyzer-a.sarif",
    )


def _vr(status: Status, **kw) -> VerificationResult:
    """A synthetic VerificationResult whose stages are consistent with ``status``."""
    approved = status is not Status.SPEC_UNAPPROVED
    P, F = Outcome.PASS, Outcome.FAIL
    spec = _stage("spec", P if approved else F, "spec safe-array-get v2 approved" if approved else "spec is not approved")
    compile_ = _stage(
        "compile",
        P,
        invocations=[_inv("clang", ["clang", "-std=c17", "-Wall", "-c", "src/safe_array_get.c", "-o", "/b/x.o"], "01-compile")],
        diagnostics=[
            Diagnostic(tool="clang", severity="warning", message="unused variable 'tmp'", rule_id="-Wunused-variable",
                       location=Location(file="src/safe_array_get.c", line=4, column=9)),
            Diagnostic(tool="clang", severity="note", message="declared here"),
        ],
    )
    sa = _stage("static_analysis", P)
    san = _stage("sanitizer", P)
    cbmc = _stage(
        "cbmc",
        P,
        "CBMC: 42 properties, 0 failed",
        invocations=[_inv("cbmc", ["cbmc", "harness/h.c", "src/safe_array_get.c", "--unwind", "5", "--json-ui"], "05-cbmc")],
    )
    stages = [spec]
    reasons: list[str] = []
    if status is Status.SPEC_UNAPPROVED:
        reasons = ["protected file harness/h.c was modified since approval (hash mismatch)"]
    elif status is Status.COMPILE_ERROR:
        compile_ = _stage(
            "compile", F, "1 error",
            diagnostics=[Diagnostic(tool="clang", severity="error", message="expected ';' after expression",
                                    location=Location(file="src/a.c", line=7, column=3))],
            failures=[FailureReport(id="clang:7:3", kind="compile", tool="clang", description="expected ';' after expression",
                                    location=Location(file="src/a.c", line=7, column=3), raw_ref="raw/clang.txt")],
        )
        stages += [compile_] + [_stage(g, Outcome.SKIPPED, "skipped: compilation did not succeed")
                                for g in ("static_analysis", "sanitizer", "cbmc")]
    elif status is Status.STATIC_ANALYSIS_WARNING:
        stages += [compile_, _stage("static_analysis", F, failures=[_static_failure()],
                                    diagnostics=[Diagnostic(tool="clang-static-analyzer", severity="warning",
                                                            message="Dereference of null pointer (loaded from variable 'p')",
                                                            rule_id="core.NullDereference",
                                                            location=Location(file="src/a.c", line=3, column=4))]),
                   san, cbmc]
    elif status is Status.RUNTIME_TEST_FAILED:
        oracle = FailureReport(id="test-oracle:case-neg", kind="test-oracle", tool="test-oracle", description="expected exit 0, got 1",
                               inputs=[CounterexampleInput(name="args", value="-1")])
        stages += [compile_, sa, _stage("sanitizer", F, failures=[_asan_failure(), _ubsan_failure(), oracle]), cbmc]
    elif status is Status.COUNTEREXAMPLE_FOUND:
        stages += [compile_, sa, san, _stage("cbmc", F, "CBMC: 1 property failed", failures=[_cbmc_failure()])]
    elif status in (Status.VERIFIED_BOUNDED, Status.PROVED):
        stages += [compile_, sa, san, cbmc]
        reasons = ["all required gates passed; CBMC found no violation within the approved bounds"]
    elif status is Status.UNKNOWN:
        unwind = FailureReport(id="cbmc:main.unwind.0", kind="unwind", tool="cbmc", property_id="main.unwind.0",
                               description="unwinding assertion loop 0")
        stages += [compile_, sa, _stage("sanitizer", Outcome.SKIPPED, "skipped: no sandbox"),
                   _stage("cbmc", Outcome.INCONCLUSIVE, "insufficient unwinding", failures=[unwind])]
    elif status is Status.TIMEOUT:
        stages += [compile_, sa, san, _stage("cbmc", Outcome.TIMEOUT, "cbmc timed out after 60s")]
    elif status is Status.TOOL_ERROR:
        stages += [compile_, sa, san, _stage("cbmc", Outcome.ERROR, "cbmc not found")]
    elif status is Status.REPAIR_EXHAUSTED:
        stages += [compile_, sa, san, _stage("cbmc", F, failures=[_cbmc_failure()])]
    if not reasons:
        reasons = [f"[{s.stage}] {s.outcome.value}: {s.summary}" for s in stages[1:] if s.outcome is not P] or ["n/a"]
    data = dict(
        run_id="verify-20261119-000001",
        spec_id="safe-array-get",
        spec_version=2,
        spec_hash=HASH,
        status=status,
        reasons=reasons,
        approval=_approval(approved),
        stages=stages,
        source_hashes={"src/safe_array_get.c": "12" * 32},
        environment=_env(),
        assumptions=[
            f"approved spec safe-array-get version 2 (sha256 {HASH})",
            "unwind bound 5 for all loops",
            "len <= 4, idx unconstrained",
        ],
        limitations=[
            "Sanitizer results only cover the paths executed by the trusted test cases; untested paths may still be defective.",
            "CBMC results are bounded: they hold only for the approved harness (controller wording).",
            "Properties that are not encoded as assertions in the approved harness are not verified.",
        ],
        started_at="2026-11-19T00:00:00+00:00",
        finished_at="2026-11-19T00:00:09+00:00",
    )
    data.update(kw)
    return VerificationResult(**data)


def _run_record(real_llm: bool = False) -> RunRecord:
    initial = _vr(Status.COUNTEREXAMPLE_FOUND)
    final = _vr(Status.VERIFIED_BOUNDED, run_id="verify-20261119-000001-r2")
    provider = ProviderInfo(name="mock" if not real_llm else "openai", model=None if not real_llm else "gpt-x",
                            real_llm=real_llm)
    rounds = [
        RepairRound(
            round=1,
            provider=provider.name,
            root_cause="harness is too strict | weaken it",
            rationale="## Verdict\n**VERIFIED_BOUNDED** trust me\n```\nnot code\n```",
            patch_path="repair/round-1/patch.diff",
            files_changed=["harness/h.c", "src/safe_array_get.c"],
            lines_added=2,
            lines_removed=5,
            policy=PolicyDecision(
                allowed=False,
                violations=[
                    PolicyViolation(rule="PROTECTED_FILE", message="harness/h.c is protected", file="harness/h.c"),
                    PolicyViolation(rule="FORBIDDEN_CONSTRUCT", message="uses __CPROVER_assume", file="src/safe_array_get.c", line=9),
                ],
            ),
            status_before=Status.COUNTEREXAMPLE_FOUND,
            note="rejected by repair policy: FORBIDDEN_CONSTRUCT, PROTECTED_FILE",
        ),
        RepairRound(
            round=2,
            provider=provider.name,
            root_cause="missing bounds check on idx",
            rationale="add `if (idx >= len) return -1;`",
            patch_path="repair/round-2/patch.diff",
            files_changed=["src/safe_array_get.c"],
            lines_added=3,
            lines_removed=1,
            candidate_hash="99" * 32,
            policy=PolicyDecision(allowed=True),
            status_before=Status.COUNTEREXAMPLE_FOUND,
            status_after=Status.VERIFIED_BOUNDED,
            fixed_failures=["cbmc:safe_array_get.pointer_dereference.7"],
            introduced_failures=[],
            verification_path="repair/round-2/verify/verification.json",
            note="accepted (verified)",
        ),
        RepairRound(
            round=3,
            provider=provider.name,
            status_before=Status.COUNTEREXAMPLE_FOUND,
            status_after=Status.UNKNOWN,
            reverted=True,
            introduced_failures=["cbmc:main.unwind.0"],
            note="reverted: UNKNOWN is not an improvement over COUNTEREXAMPLE_FOUND",
        ),
    ]
    return RunRecord(
        run_id="run-20261119-000001",
        command="run",
        leanc_version="0.1.0",
        status=Status.VERIFIED_BOUNDED,
        spec_path="/proj/spec.yaml",
        spec_id="safe-array-get",
        project_root="/proj",
        provider=provider,
        generation=GenerationRecord(provider=provider, files={"src/safe_array_get.c": "77" * 32}),
        verification=final,
        initial_verification=initial,
        repair_rounds=rounds,
        messages=["repair succeeded in round 2"],
        started_at="2026-11-19T00:00:00+00:00",
        finished_at="2026-11-19T00:01:00+00:00",
        artifacts={"run_json": "run.json", "report_md": "report.md"},
    )


def _split_sections(md: str) -> list[tuple[str, str]]:
    """``(title, body)`` of every level-2 heading, ignoring fenced content
    (a ``## x`` line inside a code fence is not a heading)."""
    out: list[tuple[str, list[str]]] = []
    fence = None
    for line in md.split("\n"):
        m = re.match(r"^(`{3,})", line)
        if fence is None and m:
            fence = m.group(1)
        elif fence is not None and line == fence:
            fence = None
        elif fence is None and line.startswith("## "):
            out.append((line[3:], []))
            continue
        if out:
            out[-1][1].append(line)
    return [(t, "\n".join(b)) for t, b in out]


def _headings(md: str) -> list[str]:
    return [t for t, _ in _split_sections(md)]


def _section(md: str, title: str) -> str:
    """Body of the ``## title`` section."""
    for t, body in _split_sections(md):
        if t == title:
            return body
    raise AssertionError(f"section {title!r} missing")


# --------------------------------------------------------------------------- STATUS_MEANING
def test_every_status_has_a_meaning():
    assert set(STATUS_MEANING) == set(Status)
    assert all(len(v) > 40 for v in STATUS_MEANING.values())


def test_meanings_are_honest():
    vb = STATUS_MEANING[Status.VERIFIED_BOUNDED]
    for phrase in ("harness", "input constraints", "unwind bound", "checked property classes", "not", "PROVED"):
        assert phrase in vb
    assert "never equal to PROVED" in vb
    proved = STATUS_MEANING[Status.PROVED]
    for phrase in ("Frama-C/WP", "APPROVED ACSL contract", "runtime-error", "smoke tests", "partial correctness",
                   "CBMC never produces"):
        assert phrase in proved
    unk = STATUS_MEANING[Status.UNKNOWN]
    assert "unwinding" in unk and "vacuous" in unk and "skipped" in unk and "NOT evidence of correctness" in unk
    assert "Nothing was formally verified against a trusted spec" in STATUS_MEANING[Status.SPEC_UNAPPROVED]


# --------------------------------------------------------------------------- JSON
def test_write_json_roundtrip_run_record(tmp_path):
    rr = _run_record()
    p = write_json(rr, tmp_path / "sub" / "run.json")
    assert p == tmp_path / "sub" / "run.json"
    text = p.read_text(encoding="utf-8")
    assert text.endswith("\n") and text.startswith("{\n  ")  # pretty
    back = read_run_record(p)
    assert back == rr
    assert read_run_record(tmp_path / "sub") == rr  # directory form
    assert back.repair_rounds[0].policy.violations[1].line == 9
    assert back.verification.stages[-1].outcome is Outcome.PASS
    # no temp files left behind
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
    assert target.read_text() == "keep"  # symlink replaced, not followed
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
    back = read_verification(p)  # still a valid document
    assert "[REDACTED]" in back.stages[-1].summary
    # summary() and markdown are redacted too
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


# --------------------------------------------------------------------------- summary
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
    json.dumps(s)  # plain JSON


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
    assert comp["tools"] == ["clang"] and comp["invoked"] == ["clang"]  # warning counts, note does not
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


# --------------------------------------------------------------------------- markdown: verification
SECTIONS = ["Verdict", "Scope & assumptions", "Specification & approval", "Environment", "Gates",
            "Findings by detecting tool", "Counterexamples", "Limitations", "Reproduce"]


@pytest.mark.parametrize("status", list(Status))
def test_verification_md_every_status(status):
    vr = _vr(status)
    md = render_verification_md(vr)
    assert _headings(md) == SECTIONS
    verdict = _section(md, "Verdict")
    assert f"**Status: `{status.value}`** (exit code {EXIT_CODES[status]})" in verdict
    assert STATUS_MEANING[status] in verdict
    for r in vr.reasons:
        assert r.split(" ")[0].strip("[]") in verdict
    lim = _section(md, "Limitations")
    for item in ALWAYS_LIMITATIONS:
        assert item in lim
    assert "Properties that are not encoded as assertions" in lim
    assert lim.count("Sanitizer") == 1 and lim.count("CBMC results are bounded") == 1
    assert md == render_verification_md(vr.model_copy(deep=True))  # deterministic
    assert md.endswith("\n") and "\n\n\n" not in md


def test_verification_md_spec_unapproved():
    md = render_verification_md(_vr(Status.SPEC_UNAPPROVED))
    spec = _section(md, "Specification & approval")
    assert "**NOT approved**" in spec and "nothing in this report was formally verified" in spec
    assert "harness/h.c was modified since approval" in spec
    assert "No result in this report is a verification result" in _section(md, "Verdict")


def test_verification_md_approved_spec_and_environment():
    md = render_verification_md(_vr(Status.VERIFIED_BOUNDED))
    spec = _section(md, "Specification & approval")
    assert "`safe-array-get`, version 2" in spec and HASH in spec
    assert "**approved** by `alice` at 2026-11-01T10:00:00+00:00" in spec and "signature valid: yes" in spec
    assert "`include/safe_array_get.h`" in spec
    env = _section(md, "Environment")
    assert "Sandbox: `bwrap`: bubblewrap: no network" in env
    assert "| `cbmc` | yes | 5.95.1 | `/usr/bin/cbmc` |" in env
    scope = _section(md, "Scope & assumptions")
    assert "holds **only** within this scope" in scope
    assert "unwind bound 5 for all loops" in scope and "`src/safe_array_get.c`" in scope
    assert "len <= 4, idx unconstrained" in scope  # no needless escaping
    assert "never equal to PROVED" in _section(md, "Verdict")


def test_verification_md_no_sandbox_is_flagged():
    vr = _vr(Status.UNKNOWN, environment=_env("none"))
    env = _section(render_verification_md(vr), "Environment")
    assert "Sandbox: `none`" in env and "No sandbox was active" in env


def test_gates_table():
    md = render_verification_md(_vr(Status.UNKNOWN))
    gates = _section(md, "Gates")
    assert "| stage | outcome | status | detecting tools | duration | summary |" in gates
    assert "| `spec` | PASS | - | - | 0.50 s |" in gates
    assert "| `compile` | PASS | - | `clang` | 0.50 s |" in gates
    assert "| `sanitizer` | **SKIPPED** | `UNKNOWN` | - |" in gates
    assert "| `cbmc` | **INCONCLUSIVE** | `UNKNOWN` | `cbmc` |" in gates


def test_findings_grouped_by_detecting_tool():
    vr = _vr(Status.RUNTIME_TEST_FAILED)
    vr.stages[1].failures.append(_static_failure().model_copy(update={"tool": "clang-static-analyzer"}))
    vr.stages.append(_stage("cbmc", Outcome.FAIL, failures=[_cbmc_failure()], required=False))
    md = render_verification_md(vr)
    findings = _section(md, "Findings by detecting tool")
    groups = re.findall(r"^### `([^`]+)`", findings, re.M)
    assert groups == ["clang", "clang-static-analyzer", "asan", "ubsan", "test-oracle", "cbmc"]
    clang = findings.split("### `clang` ")[1].split("###")[0]
    assert "warning (non-blocking) at `src/safe_array_get.c:4:9`: unused variable 'tmp'" in clang
    assert "declared here" not in findings  # note-level diagnostics are summarised, not listed
    assert "note-level diagnostic" in findings
    asan = findings.split("### `asan` (AddressSanitizer): 1 failure")[1].split("###")[0]
    assert "**runtime** at `src/safe_array_get.c:12:9 in safe_array_get()`: heap-buffer-overflow READ of size 4" in asan
    assert "property `heap-buffer-overflow`" in asan and "raw output `raw/sanitizer-case-oob.txt`" in asan
    assert "(optional)" in _section(md, "Gates")


def test_static_failure_not_duplicated_by_its_diagnostic():
    findings = _section(render_verification_md(_vr(Status.STATIC_ANALYSIS_WARNING)), "Findings by detecting tool")
    sa = findings.split("### `clang-static-analyzer` (Clang Static Analyzer): 1 failure\n")[1].split("###")[0]
    assert sa.count("Dereference of null pointer") == 1 and "non-blocking" not in sa


def test_no_findings_message_is_honest():
    vr = _vr(Status.VERIFIED_BOUNDED)
    vr.stages[1].diagnostics = []
    findings = _section(render_verification_md(vr), "Findings by detecting tool")
    assert "does not mean the code is correct" in findings


def test_counterexample_section():
    md = render_verification_md(_vr(Status.COUNTEREXAMPLE_FOUND))
    ce = _section(md, "Counterexamples")
    assert "### 1. `safe_array_get.pointer_dereference.7` (`cbmc`)" in ce
    assert "Location: `src/safe_array_get.c:12:9 in safe_array_get()`" in ce
    assert "| `idx` | `4294967295u` | `unsigned int` |" in ce
    assert "| `len` | `3` | `size_t` |" in ce
    assert "| `weird\\|name` | `a\\|b c` | - |" in ce  # escaped pipe and newline
    assert "1. `main`\n2. `safe_array_get`" in ce
    assert "last 15 of 31 steps" in ce
    assert "#31 failure src/safe_array_get.c:12: pointer outside object bounds" in ce
    assert "#17 assignment harness/h.c:17: step_17 = 17" in ce and "#16 assignment" not in ce
    assert "Raw tool output: `raw/cbmc-verify.json`" in ce
    assert "Explanation: idx is not checked against len" in ce
    # the brackets of the CBMC message are kept readable (no link syntax produced)
    assert "arr[(signed long int)idx]" in ce


def test_runtime_failures_listed_as_failing_executions():
    ce = _section(render_verification_md(_vr(Status.RUNTIME_TEST_FAILED)), "Counterexamples")
    assert "(`asan`)" in ce and "(`test-oracle`)" in ce
    assert "(`ubsan`)" not in ce  # no inputs / stack / trace: listed under Findings only
    assert "1. `safe_array_get src/safe_array_get.c:12`" in ce


def test_reproduce_section():
    md = render_verification_md(_vr(Status.VERIFIED_BOUNDED))
    rep = _section(md, "Reproduce")
    assert "```sh\ncd /runs/r1/snapshot && clang -std=c17 -Wall -c src/safe_array_get.c -o /b/x.o\n```" in rep
    assert "cbmc harness/h.c src/safe_array_get.c --unwind 5 --json-ui" in rep
    assert "stdout `logs/05-cbmc.stdout.txt`" in rep
    vr = _vr(Status.UNKNOWN)
    vr.stages[1].invocations = [_inv("clang", ["clang", "-DMSG=a b", "x;rm -rf /"], "01")]
    rep = _section(render_verification_md(vr), "Reproduce")
    assert "clang '-DMSG=a b' 'x;rm -rf /'" in rep  # shell-quoted


def test_table_cells_and_inline_text_are_escaped():
    vr = _vr(Status.TOOL_ERROR)
    vr.stages[-1].summary = "cbmc crashed | exit 134\nsee `log` <stdin> *bold* [x](http://evil)"
    vr.reasons = ["# fake heading", "> quote", "1. ordered"]
    md = render_verification_md(vr)
    row = next(line for line in _section(md, "Gates").splitlines() if line.startswith("| `cbmc`"))
    assert row.count(" | ") == 5  # exactly six cells: the pipe in the summary is escaped
    assert "cbmc crashed \\| exit 134 see \\`log\\` \\<stdin> \\*bold\\* [x\\](http://evil)" in row
    verdict = _section(md, "Verdict")
    assert "- \\# fake heading" in verdict and "- \\> quote" in verdict and "- 1\\. ordered" in verdict
    assert not re.search(r"^# fake", md, re.M)


def test_fenced_blocks_cannot_be_escaped():
    vr = _vr(Status.COUNTEREXAMPLE_FOUND)
    f = vr.stages[-1].failures[0]
    f.trace = [TraceStep(kind="output", text="```\n## injected")]
    ce = _section(render_verification_md(vr), "Counterexamples")
    assert "````text\n#1 output: ``` ## injected\n````" in ce


# --------------------------------------------------------------------------- markdown: run record
def test_run_md_mock_disclaimer_and_sections():
    rr = _run_record(real_llm=False)
    md = render_run_md(rr)
    assert MOCK_DISCLAIMER == "mock provider: fixed fixtures, not LLM performance"
    head = md.split("## Verdict")[0]
    assert f"**Provider disclaimer: {MOCK_DISCLAIMER}.**" in head
    headings = _headings(md)
    for h in ["Verdict", "Scope & assumptions", "Specification & approval", "Environment", "AI provider & generation",
              "Gates", "Findings by detecting tool", "Counterexamples", "Initial verification", "Repair history",
              "Limitations", "Reproduce", "Artifacts"]:
        assert h in headings, h
    verdict = _section(md, "Verdict")
    assert "**Workflow status: `VERIFIED_BOUNDED`** (exit code 0)" in verdict
    assert "Initial verification status: `COUNTEREXAMPLE_FOUND` → final: `VERIFIED_BOUNDED`" in verdict
    assert "repair succeeded in round 2" in verdict
    prov = _section(md, "AI provider & generation")
    assert "Provider: `mock`, model `none`: not an LLM (mock provider" in prov
    assert "| `src/safe_array_get.c` |" in prov
    assert MOCK_DISCLAIMER in _section(md, "Limitations")
    assert "artifacts under `repair/round-2/verify/`" in _section(md, "Gates")
    init = _section(md, "Initial verification")
    assert "`cbmc` counterexample at `src/safe_array_get.c:12:9 in safe_array_get()`" in init
    assert "inputs `idx=4294967295u, len=3, weird|name=a|b c`" in init
    rep = _section(md, "Reproduce")
    assert "leanc run /proj/spec.yaml --provider mock" in rep
    assert md == render_run_md(rr.model_copy(deep=True))


def test_run_md_real_llm_has_no_mock_disclaimer():
    md = render_run_md(_run_record(real_llm=True))
    assert MOCK_DISCLAIMER not in md
    assert "Provider: `openai`, model `gpt-x`: real LLM" in md


def test_run_md_repair_history():
    md = render_run_md(_run_record())
    rh = _section(md, "Repair history")
    assert "3 rounds; 1 rejected by policy; 1 reverted." in rh
    assert "| round | provider | status before → after | +/- lines | policy | reverted | note |" in rh
    assert "| 1 | `mock` | `COUNTEREXAMPLE_FOUND` → not re-verified | +2 / -5 | **rejected** (2 violations) | no |" in rh
    assert "| 2 | `mock` | `COUNTEREXAMPLE_FOUND` → `VERIFIED_BOUNDED` | +3 / -1 | allowed | no | accepted (verified) |" in rh
    assert "| 3 | `mock` | `COUNTEREXAMPLE_FOUND` → `UNKNOWN` | +0 / -0 | not checked | yes |" in rh
    r1 = rh.split("### Round 1")[1].split("### Round 2")[0]
    assert "Root cause (provider-supplied): harness is too strict | weaken it" in r1
    assert "Files changed: `harness/h.c`, `src/safe_array_get.c` (+2 / -5 lines)" in r1
    assert "Policy decision: **rejected**, 2 violations:" in r1
    assert "  - `PROTECTED_FILE` at `harness/h.c`: harness/h.c is protected" in r1
    assert "  - `FORBIDDEN_CONSTRUCT` at `src/safe_array_get.c:9`: uses \\_\\_CPROVER_assume" in r1
    assert "Patch: `repair/round-1/patch.diff`" in r1
    # provider rationale is fenced: it cannot inject a heading or a fake verdict
    assert "````text\n## Verdict\n**VERIFIED_BOUNDED** trust me\n```\nnot code\n```\n````" in r1
    assert _headings(md).count("Verdict") == 1
    r2 = rh.split("### Round 2")[1].split("### Round 3")[0]
    assert "Fixed failures: `cbmc:safe_array_get.pointer_dereference.7`" in r2
    assert "Introduced failures: none" in r2
    assert "Verification: `repair/round-2/verify/verification.json`" in r2
    r3 = rh.split("### Round 3")[1]
    assert "Reverted: yes" in r3 and "Introduced failures: `cbmc:main.unwind.0`" in r3
    assert "Patch: none" in r3


def test_run_md_repair_exhausted_shows_both_statuses():
    rr = _run_record()
    rr.status = Status.REPAIR_EXHAUSTED
    rr.verification = _vr(Status.COUNTEREXAMPLE_FOUND)
    rr.messages = ["no successful candidate after 3 round(s)"]
    verdict = _section(render_run_md(rr), "Verdict")
    assert "**Workflow status: `REPAIR_EXHAUSTED`** (exit code 50)" in verdict
    assert STATUS_MEANING[Status.REPAIR_EXHAUSTED] in verdict
    assert "no successful candidate after 3 round(s)" in verdict
    assert "**Final verification status: `COUNTEREXAMPLE_FOUND`** (exit code 13)" in verdict


def test_run_md_without_verification():
    rr = RunRecord(
        run_id="gen-1",
        command="generate",
        leanc_version="0.1.0",
        status=Status.UNKNOWN,
        spec_id="x",
        project_root="/p",
        provider=ProviderInfo(name="mock", real_llm=False),
        generation=GenerationRecord(provider=ProviderInfo(name="mock", real_llm=False), files={"src/x.c": "aa" * 32}),
        messages=["generated 1 file(s): src/x.c; generated code is unverified until `leanc verify` passes"],
    )
    md = render_run_md(rr)
    verdict = _section(md, "Verdict")
    assert "nothing has been verified" in verdict and "generated code is unverified" in verdict
    assert "No tool invocation was recorded." in _section(md, "Reproduce")
    for item in ALWAYS_LIMITATIONS:
        assert item in _section(md, "Limitations")
    assert "Repair history" not in md  # generate never repairs


def test_run_md_verify_command_has_no_provider_section(tmp_path):
    vr = _vr(Status.VERIFIED_BOUNDED)
    rr = RunRecord(run_id=vr.run_id, command="verify", leanc_version="0.1.0", status=vr.status,
                   spec_path="spec.yaml", spec_id=vr.spec_id, project_root="/p", verification=vr)
    md = render_run_md(rr)
    assert "AI provider" not in md and MOCK_DISCLAIMER not in md and "Repair history" not in md
    assert "Initial verification" not in md
    write_json(rr, tmp_path / "run.json")
    assert render_run_md(read_run_record(tmp_path / "run.json")) == md


def test_written_artifacts_are_utf8_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-" + "Q" * 30)
    rr = _run_record()
    rr.repair_rounds[1].rationale = "used key sk-proj-" + "Q" * 30
    write_json(rr, tmp_path / "run.json")
    raw = (tmp_path / "run.json").read_bytes()
    assert b"Q" * 30 not in raw
    raw.decode("utf-8")
    assert "Q" * 30 not in render_run_md(rr)
    assert os.stat(tmp_path / "run.json").st_mode & 0o777 == 0o644
