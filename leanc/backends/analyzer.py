from __future__ import annotations

import json
import os
import re
import shutil
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

from leanc.backends.clang import (
    BLOCKING_POLICY,
    DIAGNOSTIC_FLAGS,
    define_args,
    failure_from_diagnostic,
    problem_tail,
    sandbox_visible_path,
)
from leanc.backends.clang_diagnostics import (
    crash_reason,
    looks_like_crash,
    parse_clang_diagnostics,
)
from leanc.backends.sarif import ANALYZER_TOOL, parse_sarif
from leanc.foundation.models import (
    Diagnostic,
    FailureReport,
    Location,
    StageResult,
    ToolInvocation,
)
from leanc.foundation.redact import redact
from leanc.foundation.runner import Limits, ProcResult
from leanc.foundation.status import Outcome, outcome_to_status
from leanc.foundation.text import safe_name
from leanc.toolchain.context import BackendContext
from leanc.toolchain.cxx import compiler, compiler_missing, compiler_version

STAGE = "static_analysis"
TOOL = ANALYZER_TOOL
MIXED_FORMATS = "mixed"
UNKNOWN_RULE = "unknown"
DEFAULT_CHECKERS = "clang default checker set"

SARIF_ARGS: tuple[str, ...] = ("--analyzer-output", "sarif")
TEXT_ARGS: tuple[str, ...] = ("-Xclang", "-analyzer-output=text")

_CHECKER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*$")
_SARIF_UNSUPPORTED_RE = re.compile(
    r"(unknown argument|unsupported option|invalid value|unsupported value)[^\n]*(analyzer-output|sarif)",
    re.I,
)
_NON_CHECKER_RULE_PREFIXES = ("-W", "-R", "clang-", "linker-")


class AnalyzerOutput(StrEnum):
    SARIF = "sarif"
    TEXT = "text"


def analyzer_command(
    ctx: BackendContext, source_rel: str, output_path: Path | None, *, output_format: str = AnalyzerOutput.SARIF
) -> list[str]:
    language_compiler = compiler(ctx)
    if not language_compiler:
        raise ValueError("no clang available (ctx.tools.clang is None)")
    spec = ctx.spec
    argv = [language_compiler, "--analyze"]
    if output_format == AnalyzerOutput.SARIF:
        if output_path is None:
            raise ValueError("SARIF output needs an output path")
        argv += [*SARIF_ARGS, "-o", sandbox_visible_path(output_path)]
    elif output_format == AnalyzerOutput.TEXT:
        argv += [*TEXT_ARGS, "-o", os.devnull]
    else:
        raise ValueError(f"unknown analyzer output format {output_format!r}")
    argv += [
        f"-std={spec.language.standard}",
        *define_args(spec.compile.defines),
        *ctx.include_args(),
        *DIAGNOSTIC_FLAGS,
    ]
    for checker in spec.static_analysis.extra_checkers:
        argv += ["-Xclang", f"-analyzer-checker={checker}"]
    argv.append(source_rel)
    return argv


def _is_checker_finding(d: Diagnostic) -> bool:
    rule = d.rule_id or ""
    return d.severity in ("warning", "error") and bool(rule) and not rule.startswith(_NON_CHECKER_RULE_PREFIXES)


class _LoadedSarif(NamedTuple):
    data: dict | None
    problem: str


def _load_sarif(path: Path) -> _LoadedSarif:
    if not path.exists():
        return _LoadedSarif(None, "SARIF file was not produced")
    if not path.is_file():
        return _LoadedSarif(None, "SARIF output path is not a regular file")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _LoadedSarif(None, f"cannot read SARIF file: {exc}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return _LoadedSarif(None, f"SARIF file is not valid JSON: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        return _LoadedSarif(None, "SARIF file has no 'runs' array")
    return _LoadedSarif(data, "")


def _finding_identity(d: Diagnostic) -> tuple:
    location = d.location or Location()
    return (d.rule_id, location.file, location.line, location.column, d.message)


def _finding_site(d: Diagnostic) -> tuple:
    location = d.location or Location()
    return (d.rule_id, location.file, location.line)


def _findings_missing_from_sarif(stderr_diagnostics: list[Diagnostic], sarif_findings: list[Diagnostic]) -> list[Diagnostic]:
    sarif_sites = {_finding_site(d) for d in sarif_findings}
    return [d for d in stderr_diagnostics if _is_checker_finding(d) and _finding_site(d) not in sarif_sites]


def _first_clang_complaint(stderr_diagnostics: list[Diagnostic]) -> str | None:
    complaints = [
        d.message for d in stderr_diagnostics if not _is_checker_finding(d) and d.severity in ("error", "warning")
    ]
    return complaints[0] if complaints else None


def _clang_lacks_sarif_output(proc: ProcResult) -> bool:
    return not proc.error and bool(_SARIF_UNSUPPORTED_RE.search(proc.stderr or ""))


def _run_problem(proc: ProcResult, rel: str, diagnostics: list[Diagnostic]) -> str | None:
    if proc.error:
        return f"{rel}: {proc.error}"
    if looks_like_crash(proc.stderr, proc.signal, proc.exit_code):
        return f"{rel}: analyzer crashed ({crash_reason(proc.stderr, proc.signal, proc.exit_code)})"
    errors = [d for d in diagnostics if d.severity == "error" and not _is_checker_finding(d)]
    if proc.exit_code == 0 and not errors:
        return None
    first = ""
    if errors:
        error = errors[0]
        first = f": {(error.location.short() + ': ') if error.location else ''}{error.message}"
    return f"{rel}: analyzer failed (exit {proc.exit_code}){first}"


def _remove_previous_output(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


@dataclass
class _AnalysisRun:
    ctx: BackendContext
    snapshot: Path
    out_root: Path
    raw_root: Path
    limits: Limits
    files: list[dict]
    notes: list[str]
    diagnostics: list[Diagnostic] = field(default_factory=list)
    findings: list[tuple[Diagnostic, str | None]] = field(default_factory=list)
    seen_findings: set[tuple] = field(default_factory=set)
    invocations: list[ToolInvocation] = field(default_factory=list)
    timeouts: list[str] = field(default_factory=list)
    tool_errors: list[str] = field(default_factory=list)
    formats: set[str] = field(default_factory=set)

    def analyse_source(self, index: int, rel: str) -> None:
        entry: dict = {"source": rel}
        self.files.append(entry)
        if not (self.snapshot / rel).is_file():
            self.tool_errors.append(f"{rel}: source missing from the verification snapshot; not analysed")
            entry["missing"] = True
            return
        stem = f"{index:02d}-{safe_name(rel)}"
        fallback_reason = self._analyse_with_sarif(rel, stem, entry)
        if fallback_reason is not None:
            self._analyse_with_text(rel, stem, entry, fallback_reason)

    def overall_format(self) -> str | None:
        if not self.formats:
            return None
        return next(iter(self.formats)) if len(self.formats) == 1 else MIXED_FORMATS

    def _run_analyzer(self, argv: list[str], what: str) -> ProcResult:
        proc = self.ctx.run(argv, limits=self.limits, cwd=self.snapshot)
        self.invocations.append(proc.to_invocation(TOOL, self.ctx.run_dir, self.ctx.log_name(STAGE, what)))
        return proc

    def _add_new_findings(self, items: list[Diagnostic], raw_ref: str | None) -> int:
        added = 0
        for d in items:
            identity = _finding_identity(d)
            if identity in self.seen_findings:
                continue
            self.seen_findings.add(identity)
            self.diagnostics.append(d)
            self.findings.append((d, raw_ref))
            added += 1
        return added

    def _record_problem(self, problem: str, diagnostics: list[Diagnostic]) -> None:
        self.tool_errors.append(problem)
        self.diagnostics.extend(d for d in diagnostics if d.severity == "error")

    def _analyse_with_sarif(self, rel: str, stem: str, entry: dict) -> str | None:
        sarif_path = self.out_root / f"{stem}.sarif"
        _remove_previous_output(sarif_path)
        argv = analyzer_command(self.ctx, rel, sarif_path, output_format=AnalyzerOutput.SARIF)
        proc = self._run_analyzer(argv, f"{rel}.sarif")
        entry.update({"argv": proc.display_argv or argv, "exit_code": proc.exit_code, "timed_out": proc.timed_out})
        if proc.timed_out:
            self.timeouts.append(rel)
            return None
        stderr_diagnostics = parse_clang_diagnostics(proc.stderr, tool=TOOL, base_dir=self.snapshot)
        problem = _run_problem(proc, rel, stderr_diagnostics)
        if problem and _clang_lacks_sarif_output(proc):
            return "this clang does not support SARIF analyzer output"
        if problem:
            self._record_problem(problem, stderr_diagnostics)
            return None
        loaded = _load_sarif(sarif_path)
        if loaded.data is None:
            complaint = _first_clang_complaint(stderr_diagnostics)
            return loaded.problem if complaint is None else f"{loaded.problem} (clang said: {complaint})"
        self._record_sarif_findings(rel, stem, entry, proc, loaded.data, sarif_path, stderr_diagnostics)
        return None

    def _record_sarif_findings(
        self,
        rel: str,
        stem: str,
        entry: dict,
        proc: ProcResult,
        data: dict,
        sarif_path: Path,
        stderr_diagnostics: list[Diagnostic],
    ) -> None:
        raw_copy = self.raw_root / f"{stem}.sarif"
        shutil.copyfile(sarif_path, raw_copy)
        raw_ref = self.ctx.relrun(raw_copy)
        entry["sarif"] = raw_ref
        sarif_findings = parse_sarif(data, base_dir=self.snapshot, tool=TOOL)
        added = self._add_new_findings(sarif_findings, raw_ref)
        missing_in_sarif = _findings_missing_from_sarif(stderr_diagnostics, sarif_findings)
        if missing_in_sarif:
            stderr_raw = self.raw_root / f"{stem}.stderr.txt"
            stderr_raw.write_text(redact(proc.stderr), encoding="utf-8")
            added += self._add_new_findings(missing_in_sarif, self.ctx.relrun(stderr_raw))
            self.notes.append(
                f"{rel}: {len(missing_in_sarif)} finding(s) printed by the analyzer were missing from "
                "the SARIF file and were taken from its textual output"
            )
        entry["format"] = AnalyzerOutput.SARIF.value
        entry["findings"] = added
        self.formats.add(AnalyzerOutput.SARIF.value)

    def _analyse_with_text(self, rel: str, stem: str, entry: dict, fallback_reason: str) -> None:
        self.notes.append(f"{rel}: SARIF output unusable ({fallback_reason}); fell back to textual analyzer output")
        argv = analyzer_command(self.ctx, rel, None, output_format=AnalyzerOutput.TEXT)
        proc = self._run_analyzer(argv, f"{rel}.text")
        entry.update(
            {"fallback_reason": fallback_reason, "text_argv": proc.display_argv or argv, "text_exit_code": proc.exit_code}
        )
        if proc.timed_out:
            self.timeouts.append(rel)
            return
        diagnostics = parse_clang_diagnostics(proc.stderr, tool=TOOL, base_dir=self.snapshot)
        problem = _run_problem(proc, rel, diagnostics)
        if problem:
            self._record_problem(problem + " (textual fallback)", diagnostics)
            return
        raw_text = self.raw_root / f"{stem}.analyzer.txt"
        raw_text.write_text(redact(proc.stderr), encoding="utf-8")
        raw_ref = self.ctx.relrun(raw_text)
        entry["text"] = raw_ref
        entry["format"] = AnalyzerOutput.TEXT.value
        entry["findings"] = self._add_new_findings([d for d in diagnostics if _is_checker_finding(d)], raw_ref)
        self.formats.add(AnalyzerOutput.TEXT.value)


def _initial_details(ctx: BackendContext) -> dict:
    static_analysis = ctx.spec.static_analysis
    return {
        "clang": compiler(ctx),
        "clang_version": compiler_version(ctx),
        "policy": static_analysis.policy,
        "checkers": {"default": DEFAULT_CHECKERS, "extra": list(static_analysis.extra_checkers)},
        "format": None,
        "files": [],
        "findings": 0,
        "notes": [],
        "sandbox": ctx.sandbox.kind,
    }


def _setup_problem(ctx: BackendContext) -> str | None:
    spec = ctx.spec
    if not compiler(ctx):
        return compiler_missing(
            spec,
            f"clang not found (spec.compile.compiler={spec.compile.compiler!r}); the static "
            "analyzer is part of clang -- install clang or point LEANC_CLANG at it",
        )
    version = compiler_version(ctx)
    if version and "clang" not in version.lower():
        return f"{compiler(ctx)} does not identify as clang ({version!r}); cannot run the Clang Static Analyzer"
    invalid = [checker for checker in spec.static_analysis.extra_checkers if not _CHECKER_NAME_RE.match(checker)]
    if invalid:
        return f"invalid static_analysis.extra_checkers name(s): {invalid}"
    return None


def _prepare_run(ctx: BackendContext, details: dict) -> _AnalysisRun:
    out_root = ctx.build_dir / "analyzer"
    raw_root = ctx.raw_dir / "static_analysis"
    out_root.mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)
    return _AnalysisRun(
        ctx=ctx,
        snapshot=Path(sandbox_visible_path(ctx.snapshot_dir)),
        out_root=out_root,
        raw_root=raw_root,
        limits=Limits(timeout_s=ctx.timeout(ctx.spec.static_analysis.timeout_s)),
        files=details["files"],
        notes=details["notes"],
    )


def _findings_by_rule(run: _AnalysisRun) -> dict[str, int]:
    return dict(Counter(d.rule_id or UNKNOWN_RULE for d, _ in run.findings))


def _blocking_failures(run: _AnalysisRun, policy: str) -> list[FailureReport]:
    if policy != BLOCKING_POLICY:
        return []
    seen_ids: set[str] = set()
    return [
        failure_from_diagnostic(d, kind="static", tool=TOOL, id_prefix="static", seen=seen_ids, raw_ref=raw_ref)
        for d, raw_ref in run.findings
    ]


def _first_finding_text(run: _AnalysisRun) -> str:
    if not run.findings:
        return ""
    first = run.findings[0][0]
    where = first.location.short() + ": " if first.location else ""
    return f"; first: {where}{first.message} [{first.rule_id}]"


def _verdict(run: _AnalysisRun, policy: str, overall_format: str | None, editable_count: int) -> tuple[Outcome, str]:
    format_note = [f"format: {overall_format}"] if overall_format and overall_format != AnalyzerOutput.SARIF else []
    tail = problem_tail(run.timeouts, run.tool_errors, *format_note)
    count = len(run.findings)
    first = _first_finding_text(run)
    if policy == BLOCKING_POLICY and count:
        return Outcome.FAIL, f"static analyzer reported {count} finding(s) (policy=block){first}{tail}"
    if run.timeouts:
        return (
            Outcome.TIMEOUT,
            f"static analyzer timed out after {run.limits.timeout_s:g}s on {', '.join(run.timeouts)}"
            f"{tail if run.tool_errors else ''}",
        )
    if run.tool_errors:
        return Outcome.ERROR, "static analyzer failure: " + "; ".join(run.tool_errors)
    if count:
        return Outcome.PASS, f"static analyzer reported {count} finding(s); not blocking (policy=report){first}{tail}"
    return Outcome.PASS, f"static analyzer: no findings in {editable_count} file(s){tail}"


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


def run_static_analysis(ctx: BackendContext) -> StageResult:
    started = time.monotonic()
    details = _initial_details(ctx)
    setup_problem = _setup_problem(ctx)
    if setup_problem is not None:
        return _stage_result(started, details, Outcome.ERROR, setup_problem)
    run = _prepare_run(ctx, details)
    editable = ctx.spec.sources.editable
    for index, rel in enumerate(editable):
        run.analyse_source(index, rel)
    details["format"] = run.overall_format()
    details["findings"] = len(run.findings)
    details["by_rule"] = _findings_by_rule(run)
    details["timeouts"] = run.timeouts
    details["tool_errors"] = run.tool_errors
    policy = ctx.spec.static_analysis.policy
    failures = _blocking_failures(run, policy)
    outcome, summary = _verdict(run, policy, details["format"], len(editable))
    return _stage_result(
        started,
        details,
        outcome,
        summary,
        diagnostics=run.diagnostics,
        failures=failures,
        invocations=run.invocations,
    )
