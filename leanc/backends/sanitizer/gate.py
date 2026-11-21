from __future__ import annotations

import math
import os
import shlex
import shutil
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from leanc.backends.clang import MISSING_SOURCE_RULE, safe_name, unique_id
from leanc.backends.sanitizer.build import (
    build_command,
    compiler_crash_reported,
    parse_build_diagnostics,
)
from leanc.backends.sanitizer.control_scan import (
    POLICY_TOOL,
    SANITIZER_CONTROL_RULE,
    scan_sanitizer_control,
)
from leanc.backends.sanitizer.judge import (
    CaseStatus,
    CaseVerdict,
    ExecutedCase,
    judge_case,
)
from leanc.backends.sanitizer.plan import (
    SanitizerPlan,
    exit_code_details,
    find_llvm_symbolizer,
    sanitizer_env,
    sanitizer_plan,
)
from leanc.backends.sanitizer.runtime_reports import parse_sanitizer_findings
from leanc.backends.termination import signal_name, untrusted_program_terminating_signal
from leanc.foundation.models import (
    CounterexampleInput,
    Diagnostic,
    FailureReport,
    Location,
    StageResult,
    ToolInvocation,
)
from leanc.foundation.redact import redact
from leanc.foundation.runner import Limits, ProcResult
from leanc.foundation.sandbox import SandboxMounts
from leanc.foundation.status import Outcome, Status, outcome_to_status
from leanc.specs.model import TestCase, TestsConfig
from leanc.toolchain.context import BackendContext
from leanc.toolchain.cxx import compiler, compiler_missing, compiler_version

STAGE = "sanitizer"
LIMITATION = "sanitizers only observe the executed test paths; absence of findings is not a proof"
TEST_BINARY_NAME = "test_bin"
CASE_NAME_MAX_LENGTH = 60
DEFAULT_CASE_STEM = "case"
UNTRUSTED_MAX_FILE_MB = 64
STDIN_INPUT_MAX_CHARS = 2000
BUILD_FAILURE_EXPLANATION = (
    "The generated code must provide every symbol the trusted test driver uses "
    "(and nothing that clashes with it, such as a second main())."
)
SANITIZER_CONTROL_EXPLANATION = (
    "Sanitizer verdicts are only meaningful if the code under test cannot influence the "
    "sanitizer runtime; remove this construct."
)
MISSING_SYMBOLIZER_WARNING = "llvm-symbolizer not found under /usr: stack frames lack file:line"


def _real_path(path: Path) -> Path:
    return Path(os.path.realpath(path))


def _run_relative(ctx: BackendContext, path: Path) -> str:
    return os.path.relpath(os.path.realpath(path), os.path.realpath(ctx.run_dir))


def _case_stem(index: int, case: TestCase) -> str:
    return f"{index:02d}-{safe_name(case.name, CASE_NAME_MAX_LENGTH) or DEFAULT_CASE_STEM}"


def _address_space_limit_mb(plan: SanitizerPlan, tests: TestsConfig) -> int | None:
    return None if plan.uses_shadow_runtime else tests.memory_mb


def _case_inputs(case: TestCase) -> list[CounterexampleInput]:
    inputs = [
        CounterexampleInput(name="test_case", value=case.name),
        CounterexampleInput(name="argv", value=shlex.join(case.args)),
    ]
    if case.stdin is not None:
        inputs.append(CounterexampleInput(name="stdin", value=case.stdin[:STDIN_INPUT_MAX_CHARS]))
    return inputs


@dataclass
class _Layout:
    snapshot: Path
    out_root: Path
    raw_root: Path
    project_files: set[str]

    @property
    def binary(self) -> Path:
        return self.out_root / TEST_BINARY_NAME

    @property
    def cases_root(self) -> Path:
        return self.out_root / "cases"


@dataclass
class _CaseTally:
    failed: int = 0
    timed_out: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class _SanitizerRun:
    ctx: BackendContext
    plan: SanitizerPlan
    details: dict
    started: float = field(default_factory=time.monotonic)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    failures: list[FailureReport] = field(default_factory=list)
    invocations: list[ToolInvocation] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)
    control: list[Diagnostic] = field(default_factory=list)
    tally: _CaseTally = field(default_factory=_CaseTally)

    def result(self, outcome: Outcome, summary: str, status_override: Status | None = None) -> StageResult:
        return StageResult(
            stage=STAGE,
            outcome=outcome,
            status=status_override if status_override is not None else outcome_to_status(STAGE, outcome),
            summary=summary,
            diagnostics=self.diagnostics,
            failures=self.failures,
            invocations=self.invocations,
            details=self.details,
            duration_s=round(time.monotonic() - self.started, 3),
        )


def _initial_details(ctx: BackendContext, plan: SanitizerPlan) -> dict:
    details: dict = {
        "sanitizers": list(plan.effective),
        "requested_sanitizers": list(plan.requested),
        "fsanitize": list(plan.fsanitize),
        "compiler": compiler(ctx),
        "compiler_version": compiler_version(ctx),
        "sandbox": ctx.sandbox.kind,
        "limitation": LIMITATION,
        "cases": [],
    }
    if plan.notes:
        details["sanitizer_notes"] = list(plan.notes)
    return details


def _prepare_layout(ctx: BackendContext) -> _Layout:
    spec = ctx.spec
    layout = _Layout(
        snapshot=_real_path(ctx.snapshot_dir),
        out_root=_real_path(ctx.build_dir) / "sanitizer",
        raw_root=_real_path(ctx.raw_dir) / "sanitizer",
        project_files={*spec.sources.editable, *spec.sources.headers, spec.tests.driver},
    )
    layout.out_root.mkdir(parents=True, exist_ok=True)
    layout.raw_root.mkdir(parents=True, exist_ok=True)
    return layout


def _missing_inputs(run: _SanitizerRun, layout: _Layout) -> StageResult | None:
    spec = run.ctx.spec
    if not (layout.snapshot / spec.tests.driver).is_file():
        return run.result(
            Outcome.ERROR, f"trusted test driver {spec.tests.driver!r} is missing from the verification snapshot"
        )
    missing = [rel for rel in spec.sources.editable if not (layout.snapshot / rel).is_file()]
    if not missing:
        return None
    for rel in missing:
        message = f"editable source {rel!r} is missing from the verification snapshot"
        run.diagnostics.append(
            Diagnostic(
                tool="leanc", rule_id=MISSING_SOURCE_RULE, severity="error", message=message, location=Location(file=rel)
            )
        )
        run.failures.append(
            FailureReport(
                id=unique_id(f"sanitizer:build:{MISSING_SOURCE_RULE}:{rel}", run.seen_ids),
                kind="compile",
                tool="leanc",
                property_id=MISSING_SOURCE_RULE,
                description=message,
                location=Location(file=rel),
            )
        )
    return run.result(
        Outcome.FAIL,
        f"cannot build the sanitizer test binary: missing {', '.join(missing)}",
        status_override=Status.COMPILE_ERROR,
    )


def _report_sanitizer_control(run: _SanitizerRun, layout: _Layout) -> None:
    run.control = scan_sanitizer_control(layout.snapshot, run.ctx.spec.sources.editable)
    for d in run.control:
        run.diagnostics.append(d)
        run.failures.append(
            FailureReport(
                id=unique_id(
                    f"sanitizer:policy:{SANITIZER_CONTROL_RULE}:{d.location.file if d.location else '?'}", run.seen_ids
                ),
                kind="policy",
                tool=POLICY_TOOL,
                property_id=SANITIZER_CONTROL_RULE,
                description=d.message,
                location=d.location,
                explanation=SANITIZER_CONTROL_EXPLANATION,
            )
        )


def _discard_stale_binary(binary: Path) -> None:
    if binary.exists():
        binary.unlink()


def _build_failure(run: _SanitizerRun, proc: ProcResult, build_diagnostics: list[Diagnostic], raw_ref: str) -> StageResult:
    run.diagnostics.extend(build_diagnostics)
    first = next((d for d in build_diagnostics if d.severity == "error"), None)
    cause = f": {first.message}" if first else f" (clang exit code {proc.exit_code})"
    description = "the editable sources do not build/link against the trusted test driver with sanitizers" + cause
    run.failures.append(
        FailureReport(
            id=unique_id("sanitizer:build", run.seen_ids),
            kind="compile",
            tool="clang",
            property_id=first.rule_id if first else None,
            description=description,
            location=first.location if first else None,
            explanation=BUILD_FAILURE_EXPLANATION,
            raw_ref=raw_ref,
        )
    )
    return run.result(Outcome.FAIL, f"sanitizer build failed: {description}", status_override=Status.COMPILE_ERROR)


def _build_test_binary(run: _SanitizerRun, layout: _Layout) -> StageResult | None:
    ctx = run.ctx
    binary = layout.binary
    _discard_stale_binary(binary)
    argv = build_command(ctx, binary, run.plan)
    build_details: dict = {"argv": list(argv), "cwd": "snapshot", "binary": _run_relative(ctx, binary)}
    run.details["build"] = build_details
    timeout_s = ctx.timeout(ctx.spec.compile.timeout_s)
    proc = ctx.run(argv, limits=Limits(timeout_s=timeout_s), cwd=layout.snapshot)
    run.invocations.append(proc.to_invocation("clang", ctx.run_dir, ctx.log_name(STAGE, "build")))
    build_raw = layout.raw_root / "build.stderr.txt"
    build_raw.write_text(redact(proc.stderr), encoding="utf-8")
    build_details.update({"exit_code": proc.exit_code, "raw": _run_relative(ctx, build_raw)})
    if proc.error:
        return run.result(Outcome.ERROR, f"sanitizer build could not start: {proc.error}")
    if proc.timed_out:
        return run.result(Outcome.TIMEOUT, f"sanitizer build timed out after {timeout_s:g}s")
    build_diagnostics = parse_build_diagnostics(proc.stderr, base_dir=str(layout.snapshot))
    crash_signal = untrusted_program_terminating_signal(proc)
    if crash_signal is not None or compiler_crash_reported(proc.stderr):
        why = f"terminated by {signal_name(crash_signal)}" if crash_signal is not None else "compiler crash reported"
        return run.result(Outcome.ERROR, f"clang crashed while building the sanitizer test binary ({why})")
    build_details["warnings"] = sum(1 for d in build_diagnostics if d.severity == "warning")
    if proc.exit_code != 0 or not binary.is_file():
        return _build_failure(run, proc, build_diagnostics, _run_relative(ctx, build_raw))
    return None


def _execution_refused(run: _SanitizerRun) -> StageResult | None:
    ctx = run.ctx
    allowed, reason = ctx.may_execute_untrusted()
    run.details["sandbox_reason"] = reason
    if not allowed:
        return run.result(Outcome.SKIPPED, reason)
    if not ctx.sandbox.active:
        run.details["sandbox_warning"] = reason
    return None


def _runtime_environment(run: _SanitizerRun) -> dict[str, str]:
    ctx = run.ctx
    symbolizer = find_llvm_symbolizer(compiler(ctx), compiler_version(ctx))
    run.details["symbolizer"] = symbolizer
    if not symbolizer:
        run.details["symbolizer_warning"] = MISSING_SYMBOLIZER_WARNING
    env = sanitizer_env(run.plan, ctx.spec.tests.memory_mb, symbolizer)
    run.details["env"] = dict(env)
    run.details["exit_codes"] = exit_code_details()
    return env


def _fresh_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def _case_limits(timeout_s: float, plan: SanitizerPlan, tests: TestsConfig) -> Limits:
    return Limits(
        timeout_s=timeout_s,
        cpu_s=math.ceil(timeout_s),
        memory_mb=_address_space_limit_mb(plan, tests),
        max_output_bytes=tests.max_output_bytes,
        max_file_mb=UNTRUSTED_MAX_FILE_MB,
    )


def _case_entry(case: TestCase, proc: ProcResult, executed: ExecutedCase, case_dir: Path, raw_stdout: str,
                ctx: BackendContext) -> dict:
    return {
        "name": case.name,
        "args": list(case.args),
        "exit_code": proc.exit_code,
        "signal": executed.signal,
        "expected_exit": case.expect_exit,
        "timed_out": executed.timed_out,
        "output_truncated": proc.output_truncated,
        "findings": [],
        "stdout_matches": None,
        "duration_s": round(proc.duration_s, 3),
        "timeout_s": executed.timeout_s,
        "cwd": _run_relative(ctx, case_dir),
        "raw_stderr": executed.raw_ref,
        "raw_stdout": raw_stdout,
    }


def _apply_verdict(entry: dict, verdict: CaseVerdict) -> None:
    entry["findings"].extend(verdict.finding_summaries)
    if verdict.timeout_reason is not None:
        entry["timeout_reason"] = verdict.timeout_reason
    if verdict.stdout_matches is not None:
        entry["stdout_matches"] = verdict.stdout_matches
    entry["status"] = verdict.status.value
    if verdict.status is CaseStatus.ERROR:
        entry["error"] = verdict.error


def _run_case(run: _SanitizerRun, layout: _Layout, env: dict[str, str], index: int, case: TestCase) -> None:
    ctx, tests = run.ctx, run.ctx.spec.tests
    stem = _case_stem(index, case)
    case_dir = layout.cases_root / stem
    case_dir.mkdir()
    timeout_s = ctx.timeout(case.timeout_s or tests.timeout_s)
    proc = ctx.run(
        [str(layout.binary), *case.args],
        limits=_case_limits(timeout_s, run.plan, tests),
        cwd=case_dir,
        env_extra=env,
        stdin_data=case.stdin.encode("utf-8") if case.stdin is not None else None,
        mounts=SandboxMounts(ro=[str(layout.out_root)], rw=[str(case_dir)]),
    )
    run.invocations.append(proc.to_invocation("sanitizer-test", ctx.run_dir, ctx.log_name(STAGE, case.name)))
    err_raw = layout.raw_root / f"{stem}.stderr.txt"
    out_raw = layout.raw_root / f"{stem}.stdout.txt"
    err_raw.write_text(redact(proc.stderr), encoding="utf-8")
    out_raw.write_text(redact(proc.stdout), encoding="utf-8")
    terminating_signal = untrusted_program_terminating_signal(proc)
    cpu_limited = terminating_signal == signal.SIGXCPU
    executed = ExecutedCase(
        case=case,
        proc=proc,
        signal=terminating_signal,
        cpu_limited=cpu_limited,
        timed_out=proc.timed_out or cpu_limited,
        findings=parse_sanitizer_findings(proc.stderr, project_files=layout.project_files, base_dir=str(layout.snapshot)),
        inputs=_case_inputs(case),
        raw_ref=_run_relative(ctx, err_raw),
        timeout_s=timeout_s,
        max_output_bytes=tests.max_output_bytes,
    )
    entry = _case_entry(case, proc, executed, case_dir, _run_relative(ctx, out_raw), ctx)
    run.details["cases"].append(entry)
    verdict = judge_case(executed, run.seen_ids)
    run.diagnostics.extend(verdict.diagnostics)
    run.failures.extend(verdict.failures)
    _apply_verdict(entry, verdict)
    _tally(run.tally, case, verdict)


def _tally(tally: _CaseTally, case: TestCase, verdict: CaseVerdict) -> None:
    if verdict.status is CaseStatus.FAIL:
        tally.failed += 1
    elif verdict.status is CaseStatus.ERROR:
        tally.errors.append(verdict.error or case.name)
    elif verdict.status is CaseStatus.TIMEOUT:
        tally.timed_out.append(case.name)


def _counts(run: _SanitizerRun, total: int) -> dict:
    tally = run.tally
    return {
        "cases": total,
        "passed": total - tally.failed - len(tally.timed_out) - len(tally.errors),
        "failed": tally.failed,
        "timed_out": len(tally.timed_out),
        "errors": len(tally.errors),
        "findings": sum(len(c["findings"]) for c in run.details["cases"]),
    }


def _failed_case_description(entry: dict) -> str:
    if not entry["findings"]:
        return f"{entry['name']} (test oracle)"
    first = entry["findings"][0]
    where = f" at {first['location']}" if first["location"] else ""
    return f"{entry['name']} ({first['tool']} {first['rule_id']}{where})"


def _failure_summary(run: _SanitizerRun, total: int) -> str:
    parts = []
    if run.tally.failed:
        failed = [_failed_case_description(c) for c in run.details["cases"] if c["status"] == CaseStatus.FAIL]
        parts.append(f"{run.tally.failed} of {total} test case(s) failed: " + ", ".join(failed))
    if run.control:
        parts.append(f"{len(run.control)} sanitizer-control construct(s) in editable sources")
    return "; ".join(parts)


def _final_result(run: _SanitizerRun) -> StageResult:
    ctx = run.ctx
    total = len(ctx.spec.tests.cases)
    run.details["counts"] = _counts(run, total)
    if run.control or run.tally.failed:
        return run.result(Outcome.FAIL, _failure_summary(run, total))
    if run.tally.timed_out:
        return run.result(Outcome.TIMEOUT, f"test case(s) timed out: {', '.join(run.tally.timed_out)}")
    if run.tally.errors:
        return run.result(Outcome.ERROR, "; ".join(run.tally.errors))
    sanitizers = "+".join(run.plan.effective) or "no"
    where = f"sandbox: {ctx.sandbox.kind}" if ctx.sandbox.active else "UNSANDBOXED"
    return run.result(Outcome.PASS, f"{total}/{total} test case(s) passed under {sanitizers} sanitizer(s) ({where})")


def run_sanitizer_tests(ctx: BackendContext) -> StageResult:
    plan = sanitizer_plan(ctx.spec.tests.sanitizers)
    run = _SanitizerRun(ctx=ctx, plan=plan, details=_initial_details(ctx, plan))
    tests = ctx.spec.tests
    if not tests.driver or not tests.cases:
        return run.result(Outcome.SKIPPED, "no trusted test driver / test cases configured (tests.driver, tests.cases)")
    if not compiler(ctx):
        return run.result(
            Outcome.ERROR,
            compiler_missing(
                ctx.spec,
                f"C compiler not found (spec.compile.compiler={ctx.spec.compile.compiler!r}); "
                "install clang or set LEANC_CLANG",
            ),
        )
    layout = _prepare_layout(ctx)
    stopped = _missing_inputs(run, layout)
    if stopped is not None:
        return stopped
    _report_sanitizer_control(run, layout)
    stopped = _build_test_binary(run, layout)
    if stopped is not None:
        return stopped
    stopped = _execution_refused(run)
    if stopped is not None:
        return stopped
    env = _runtime_environment(run)
    _fresh_directory(layout.cases_root)
    for index, case in enumerate(tests.cases):
        _run_case(run, layout, env, index, case)
    return _final_result(run)
