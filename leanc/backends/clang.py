from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from leanc.backends.clang_diagnostics import (
    crash_reason,
    looks_like_crash,
    parse_clang_diagnostics,
)
from leanc.foundation.models import (
    Diagnostic,
    FailureKind,
    FailureReport,
    Location,
    StageResult,
    ToolInvocation,
    TraceStep,
)
from leanc.foundation.redact import redact
from leanc.foundation.runner import Limits, ProcResult
from leanc.foundation.status import Outcome, outcome_to_status
from leanc.foundation.text import safe_name
from leanc.toolchain.context import BackendContext
from leanc.toolchain.cxx import compiler, compiler_missing, compiler_version

STAGE = "compile"
TOOL = "clang"
MISSING_SOURCE_RULE = "missing-source"
BLOCKING_POLICY = "block"

DIAGNOSTIC_FLAGS: tuple[str, ...] = (
    "-fno-color-diagnostics",
    "-fno-caret-diagnostics",
    "-fdiagnostics-show-option",
)


def define_args(defines: list[str]) -> list[str]:
    return [d if d.startswith("-D") else f"-D{d}" for d in defines]


def sandbox_visible_path(path: Path) -> str:
    return os.path.realpath(path)


def compile_command(ctx: BackendContext, source_rel: str, obj_path: Path) -> list[str]:
    language_compiler = compiler(ctx)
    if not language_compiler:
        raise ValueError("no C compiler available (ctx.tools.clang is None)")
    spec = ctx.spec
    return [
        language_compiler,
        f"-std={spec.language.standard}",
        *spec.compile.flags,
        *define_args(spec.compile.defines),
        *ctx.include_args(),
        *DIAGNOSTIC_FLAGS,
        "-c",
        source_rel,
        "-o",
        sandbox_visible_path(obj_path),
    ]


def unique_id(base: str, seen: set[str]) -> str:
    candidate, suffix = base, 2
    while candidate in seen:
        candidate = f"{base}#{suffix}"
        suffix += 1
    seen.add(candidate)
    return candidate


def _location_key(location: Location | None) -> str:
    if location is None:
        return "?"
    parts = (location.file or "?", location.line, location.column)
    return ":".join(str(part) for part in parts if part is not None)


def failure_from_diagnostic(
    d: Diagnostic,
    *,
    kind: FailureKind,
    tool: str,
    id_prefix: str,
    seen: set[str],
    raw_ref: str | None,
) -> FailureReport:
    return FailureReport(
        id=unique_id(f"{id_prefix}:{d.rule_id or 'unknown'}:{_location_key(d.location)}", seen),
        kind=kind,
        tool=tool,
        property_id=d.rule_id,
        description=d.message,
        location=d.location,
        trace=[TraceStep(kind="location", location=s.location, text=s.message) for s in d.path],
        raw_ref=raw_ref,
    )


def problem_tail(timeouts: list[str], tool_errors: list[str], *extra: str) -> str:
    problems = []
    if timeouts:
        problems.append(f"timed out: {', '.join(timeouts)}")
    if tool_errors:
        problems.append(f"tool errors: {'; '.join(tool_errors)}")
    problems.extend(extra)
    return f" [{'; '.join(problems)}]" if problems else ""


def object_name(rel: str) -> str:
    return rel[:-2] + ".o" if rel.endswith(".c") else rel + ".o"


def _discard_stale_output(path: Path) -> None:
    if path.exists():
        path.unlink()


def _no_error_points_at_source(errors: list[Diagnostic]) -> bool:
    return not any(d.location is not None and d.location.file for d in errors)


def _first_error_text(diagnostics: list[Diagnostic]) -> str:
    for d in diagnostics:
        if d.severity == "error":
            where = d.location.short() if d.location else ""
            return f"{where}: {d.message}" if where else d.message
    return ""


@dataclass
class _CompileRun:
    ctx: BackendContext
    snapshot: Path
    obj_root: Path
    raw_root: Path
    limits: Limits
    policy: str
    files: list[dict]
    diagnostics: list[Diagnostic] = field(default_factory=list)
    failures: list[FailureReport] = field(default_factory=list)
    invocations: list[ToolInvocation] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)
    timeouts: list[str] = field(default_factory=list)
    tool_errors: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    error_diagnostics: list[Diagnostic] = field(default_factory=list)
    warning_count: int = 0
    compiled: int = 0

    def compile_source(self, index: int, rel: str) -> None:
        entry: dict = {"source": rel}
        self.files.append(entry)
        if not (self.snapshot / rel).is_file():
            self._record_missing_source(rel, entry)
            return
        obj = self.obj_root / object_name(rel)
        obj.parent.mkdir(parents=True, exist_ok=True)
        _discard_stale_output(obj)
        argv = compile_command(self.ctx, rel, obj)
        proc = self.ctx.run(argv, limits=self.limits, cwd=self.snapshot)
        self.invocations.append(proc.to_invocation(TOOL, self.ctx.run_dir, self.ctx.log_name(STAGE, rel)))
        raw_ref = self._save_stderr(index, rel, proc)
        entry.update(
            {
                "argv": proc.display_argv or argv,
                "exit_code": proc.exit_code,
                "signal": proc.signal,
                "timed_out": proc.timed_out,
                "output_truncated": proc.output_truncated,
                "raw": raw_ref,
            }
        )
        if proc.error:
            self.tool_errors.append(f"{rel}: {proc.error}")
            entry["error"] = proc.error
            return
        if proc.timed_out:
            self.timeouts.append(rel)
            return
        self._judge_output(rel, entry, proc, obj, raw_ref)

    def _save_stderr(self, index: int, rel: str, proc: ProcResult) -> str:
        raw_path = self.raw_root / f"{index:02d}-{safe_name(rel)}.stderr.txt"
        raw_path.write_text(redact(proc.stderr), encoding="utf-8")
        return self.ctx.relrun(raw_path)

    def _record_missing_source(self, rel: str, entry: dict) -> None:
        self.missing.append(rel)
        message = (
            f"editable source {rel!r} is missing from the verification snapshot "
            "(it was never generated/written, or is not a regular file)"
        )
        entry["missing"] = True
        self.diagnostics.append(
            Diagnostic(
                tool="leanc", rule_id=MISSING_SOURCE_RULE, severity="error", message=message, location=Location(file=rel)
            )
        )
        self.failures.append(
            FailureReport(
                id=unique_id(f"compile:{MISSING_SOURCE_RULE}:{rel}", self.seen_ids),
                kind="compile",
                tool="leanc",
                property_id=MISSING_SOURCE_RULE,
                description=message,
                location=Location(file=rel),
            )
        )

    def _judge_output(self, rel: str, entry: dict, proc: ProcResult, obj: Path, raw_ref: str) -> None:
        diagnostics = parse_clang_diagnostics(proc.stderr, tool=TOOL, base_dir=self.snapshot)
        self.diagnostics.extend(diagnostics)
        errors = [d for d in diagnostics if d.severity == "error"]
        warnings = [d for d in diagnostics if d.severity == "warning"]
        entry["errors"], entry["warnings"] = len(errors), len(warnings)
        if looks_like_crash(proc.stderr, proc.signal, proc.exit_code):
            self.tool_errors.append(f"{rel}: clang crashed ({crash_reason(proc.stderr, proc.signal, proc.exit_code)})")
            entry["crashed"] = True
            return
        if errors and _no_error_points_at_source(errors):
            self.tool_errors.append(f"{rel}: clang error not attributable to the source: {errors[0].message}")
            entry["driver_errors"] = [d.message for d in errors]
            return
        self.error_diagnostics.extend(errors)
        self.warning_count += len(warnings)
        blocking = errors + warnings if self.policy == BLOCKING_POLICY else errors
        for d in blocking:
            self.failures.append(
                failure_from_diagnostic(d, kind="compile", tool=TOOL, id_prefix=TOOL, seen=self.seen_ids, raw_ref=raw_ref)
            )
        if errors:
            return
        if proc.exit_code != 0:
            truncated = " (output truncated)" if proc.output_truncated else ""
            self.tool_errors.append(f"{rel}: clang exited with code {proc.exit_code} without reporting an error{truncated}")
            return
        if not obj.is_file():
            self.tool_errors.append(f"{rel}: clang reported success but produced no object file")
            return
        self.compiled += 1
        entry["object"] = self.ctx.relrun(obj)

    def totals(self) -> dict:
        return {
            "errors": len(self.error_diagnostics),
            "warnings": self.warning_count,
            "compiled": self.compiled,
            "missing_sources": self.missing,
            "timeouts": self.timeouts,
            "tool_errors": self.tool_errors,
        }

    def summary_tail(self) -> str:
        return problem_tail(self.timeouts, self.tool_errors)

    def failed_compilation_summary(self) -> str:
        parts = []
        if self.error_diagnostics:
            parts.append(f"{len(self.error_diagnostics)} error(s)")
        if self.missing:
            parts.append(f"missing source(s): {', '.join(self.missing)}")
        summary = f"compilation failed: {', '.join(parts)}, {self.warning_count} warning(s)"
        first = _first_error_text(self.error_diagnostics)
        if first:
            summary += f"; first: {first}"
        return summary


def _initial_details(ctx: BackendContext) -> dict:
    spec = ctx.spec
    return {
        "compiler": compiler(ctx),
        "compiler_version": compiler_version(ctx),
        "requested_compiler": spec.compile.compiler,
        "standard": spec.language.standard,
        "flags": list(spec.compile.flags),
        "defines": list(spec.compile.defines),
        "warnings_policy": spec.compile.warnings_policy,
        "sandbox": ctx.sandbox.kind,
        "files": [],
        "errors": 0,
        "warnings": 0,
    }


def _compiler_problem(ctx: BackendContext) -> str | None:
    spec = ctx.spec
    if not compiler(ctx):
        return compiler_missing(
            spec,
            f"C compiler not found (spec.compile.compiler={spec.compile.compiler!r}); "
            "install clang or point LEANC_CLANG at it",
        )
    version = compiler_version(ctx)
    if version and "clang" not in version.lower():
        return (
            f"compiler {compiler(ctx)} does not identify as clang ({version!r}); "
            "Lean C's compile gate requires clang"
        )
    return None


def _prepare_run(ctx: BackendContext, details: dict) -> _CompileRun:
    obj_root = ctx.build_dir / "obj"
    raw_root = ctx.raw_dir / "compile"
    obj_root.mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)
    return _CompileRun(
        ctx=ctx,
        snapshot=Path(sandbox_visible_path(ctx.snapshot_dir)),
        obj_root=obj_root,
        raw_root=raw_root,
        limits=Limits(timeout_s=ctx.timeout(ctx.spec.compile.timeout_s)),
        policy=ctx.spec.compile.warnings_policy,
        files=details["files"],
    )


def _verdict(run: _CompileRun, compiler_label: str | None) -> tuple[Outcome, str]:
    if run.error_diagnostics or run.missing:
        return Outcome.FAIL, run.failed_compilation_summary() + run.summary_tail()
    if run.policy == BLOCKING_POLICY and run.warning_count:
        return (
            Outcome.FAIL,
            f"compilation produced {run.warning_count} warning(s), blocked by policy (compile.warnings_policy=block)"
            + run.summary_tail(),
        )
    if run.timeouts:
        tool_errors = f" [tool errors: {'; '.join(run.tool_errors)}]" if run.tool_errors else ""
        return (
            Outcome.TIMEOUT,
            f"clang timed out after {run.limits.timeout_s:g}s compiling {', '.join(run.timeouts)}{tool_errors}",
        )
    if run.tool_errors:
        return Outcome.ERROR, "compiler failure: " + "; ".join(run.tool_errors)
    return (
        Outcome.PASS,
        f"compiled {run.compiled} file(s) with {compiler_label}: 0 errors, {run.warning_count} warning(s)"
        f" (warnings_policy={run.policy})",
    )


def _stage_result(started: float, details: dict, outcome: Outcome, summary: str, **evidence) -> StageResult:
    return StageResult(
        stage=STAGE,
        outcome=outcome,
        status=outcome_to_status(STAGE, outcome),
        summary=summary,
        details=details,
        duration_s=round(time.monotonic() - started, 3),
        **evidence,
    )


def run_compile(ctx: BackendContext) -> StageResult:
    started = time.monotonic()
    details = _initial_details(ctx)
    compiler_problem = _compiler_problem(ctx)
    if compiler_problem is not None:
        return _stage_result(started, details, Outcome.ERROR, compiler_problem)
    run = _prepare_run(ctx, details)
    for index, rel in enumerate(ctx.spec.sources.editable):
        run.compile_source(index, rel)
    details.update(run.totals())
    outcome, summary = _verdict(run, compiler_version(ctx) or ctx.tools.clang)
    return _stage_result(
        started,
        details,
        outcome,
        summary,
        diagnostics=run.diagnostics,
        failures=run.failures,
        invocations=run.invocations,
    )
