from __future__ import annotations

import re

from leanc.foundation.models import (
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
from leanc.foundation.status import Outcome, Status, outcome_to_status
from leanc.pipeline.controller import VERIFIED_INDUCTIVE_REASON

HASH = "ab" * 32


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
    elif status is Status.VERIFIED_INDUCTIVE:
        stages += [compile_, sa, san, cbmc, _stage("inductive", P, "inductive: 1 claimed function, 1 contracted loop")]
        reasons = [VERIFIED_INDUCTIVE_REASON]
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
    for t, body in _split_sections(md):
        if t == title:
            return body
    raise AssertionError(f"section {title!r} missing")
