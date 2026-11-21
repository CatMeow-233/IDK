from __future__ import annotations

import json
import os
import signal as _signal
import time
from pathlib import Path
from typing import Any

from leanc.backends.wp.argv import (
    MACHDEP,
    RTE_CLASSES,
    RTE_EXTRA_CHECK_DESCRIPTIONS,
    build_wp_argv,
    prover_timeout,
    rte_flags,
    shell_unsafe_preprocessor_argument,
)
from leanc.backends.wp.assumptions import wp_assumptions
from leanc.backends.wp.console import ConsoleSummary, contract_merge_warning, parse_console, warning_facts
from leanc.backends.wp.failures import (
    TOOL,
    SourceLines,
    contract_not_applied_failure,
    failure_diagnostic,
    refuted_goal_failure,
    smoke_test_failure,
    unproved_goal_failure,
)
from leanc.backends.wp.goals import (
    ContractApplication,
    GoalTally,
    contract_application,
    is_goal_list,
    tally_goals,
)
from leanc.backends.wp.installation import (
    extra_read_only_mounts,
    framac_share_dir,
    is_automatic_prover,
    prover_executable,
    tool_path,
    why3_config_provers,
)
from leanc.backends.wp.verdict import MAX_LISTED, ProofEvidence, Verdict, excerpt, final_verdict
from leanc.foundation.models import Diagnostic, FailureReport, StageResult, ToolInvocation
from leanc.foundation.redact import redact
from leanc.foundation.runner import Limits, ProcResult
from leanc.foundation.status import Outcome, outcome_to_status
from leanc.lang.c_lexer import CSource
from leanc.rules.acsl import TargetContract, acsl_violations, target_contracts
from leanc.toolchain.context import BackendContext
from leanc.toolchain.framac import INSTALL_HINT, MIN_FRAMAC_MAJOR, FramaC, find_framac, framac_version_key
from leanc.toolchain.tools import tool_version

STAGE = "wp"
MEMORY_MODEL = "Typed (WP default)"
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_FILE_MB = 1024
WHY3_DETECT_TIMEOUT_S = 60.0
SIGNAL_EXIT_CODE_BASE = 128
MAX_SIGNAL_NUMBER = 64
OUT_OF_MEMORY_SIGNALS = frozenset({_signal.SIGKILL, _signal.SIGABRT, _signal.SIGSEGV})
SIGNAL_NAMES = {sig.value: sig.name for sig in _signal.Signals}
MAX_RECORDED_WARNINGS = 100
MAX_NOTED_ACSL_VIOLATIONS = 10
MAX_MERGE_RECORD_CHARS = 300
MAX_MERGE_SUMMARY_CHARS = 200
MAX_WHY3_ERROR_TAIL = 300
MAX_CONSOLE_TAIL = 400
STDERR_SEPARATOR = "\n--- stderr ---\n"
NON_AUTOMATIC_PROVER_ADVICE = (
    "which is not an automatic prover (script replay / interactive provers could reuse proofs not produced by "
    "this run); use e.g. alt-ergo, z3, cvc5"
)


def _initial_details(ctx: BackendContext) -> dict[str, Any]:
    wp = ctx.spec.wp
    return {
        "frama_c": None,
        "frama_c_version": None,
        "why3": None,
        "why3_version": None,
        "provers": list(wp.provers) if wp else [],
        "prover_versions": {},
        "provers_detected": [],
        "prover_timeout_s": None,
        "require_termination": bool(wp and wp.require_termination),
        "machdep": MACHDEP,
        "memory_model": MEMORY_MODEL,
        "rte_checks": list(RTE_CLASSES),
        "sandbox": ctx.sandbox.kind,
        "argv": {},
        "raw": {},
        "contract": [],
        "goals_total": 0,
        "goals_proved": 0,
        "goals_by_verdict": {},
        "smoke_tests": None,
        "console": {},
        "goals": [],
        "warnings": [],
        "memory_model_hypotheses": [],
        "library_functions_with_generated_clauses": [],
        "missing_specification": [],
        "notes": [],
        "assumptions": [],
    }


def _signal_from_sandbox_exit_code(exit_code: int | None) -> int | None:
    if exit_code is not None and SIGNAL_EXIT_CODE_BASE < exit_code <= SIGNAL_EXIT_CODE_BASE + MAX_SIGNAL_NUMBER:
        return exit_code - SIGNAL_EXIT_CODE_BASE
    return None


def _terminating_signal(proc: ProcResult) -> int | None:
    if proc.signal is not None:
        return proc.signal
    return _signal_from_sandbox_exit_code(proc.exit_code)


def _signal_name(sig: int) -> str:
    return SIGNAL_NAMES.get(sig, f"signal {sig}")


def _untrusted_acsl(ctx: BackendContext, snapshot: Path, headers: dict[str, str]) -> list[str]:
    found: list[str] = []
    for rel in ctx.spec.sources.editable:
        source = CSource((snapshot / rel).read_text(encoding="utf-8", errors="replace"))
        for rule, message, offset in acsl_violations(ctx.spec, source, headers):
            found.append(f"{rel}:{source.line_of(offset)}: {rule}: {message}")
    return found


def _rejected_contract_merge(merge: str) -> Verdict:
    return Verdict(
        Outcome.ERROR,
        "Frama-C merged a second ACSL contract into an approved one ("
        + merge.strip()[:MAX_MERGE_SUMMARY_CHARS]
        + "): editable code must not add contracts to the target or to header functions; no verdict",
    )


class _WpRun:
    def __init__(self, ctx: BackendContext):
        self.started = time.monotonic()
        self.ctx = ctx
        self.spec = ctx.spec
        self.wp = ctx.spec.wp
        self.target = ctx.spec.target.function
        self.snapshot = Path(os.path.realpath(ctx.snapshot_dir))
        self.raw_root = ctx.raw_dir / STAGE
        self.work = ctx.build_dir / STAGE
        self.why3_conf = self.work / "why3.conf"
        self.report = self.raw_root / "wp-report.json"
        self.details = _initial_details(ctx)
        self.invocations: list[ToolInvocation] = []
        self.diagnostics: list[Diagnostic] = []
        self.failures: list[FailureReport] = []
        self.fc: FramaC
        self.headers: dict[str, str] = {}
        self.contracts: list[TargetContract] = []
        self.limits: Limits
        self.env: dict[str, str] = {}
        self.mounts = None
        self.deadline = 0.0
        self.why3_conf_used: Path | None = None
        self.proc: ProcResult
        self.console = ConsoleSummary()
        self.goals: list[dict] = []
        self.tally = GoalTally()

    def result(self, verdict: Verdict) -> StageResult:
        self.details["assumptions"] = wp_assumptions(self.ctx, self.details, verdict.outcome)
        return StageResult(
            stage=STAGE,
            outcome=verdict.outcome,
            status=outcome_to_status(STAGE, verdict.outcome),
            summary=verdict.summary,
            diagnostics=self.diagnostics,
            failures=self.failures,
            invocations=self.invocations,
            details=self.details,
            duration_s=round(time.monotonic() - self.started, 3),
        )

    def decide(self) -> Verdict:
        steps = (
            self._check_spec,
            self._locate_framac,
            self._check_provers,
            self._read_inputs,
            self._find_approved_contract,
            self._reject_untrusted_acsl,
            self._prepare_run,
            self._detect_why3_provers,
            self._remove_stale_report,
            self._run_framac,
            self._check_process,
            self._read_console,
            self._check_framac_exit,
            self._load_report,
            self._check_report,
            self._tally_goals,
            self._check_goal_consistency,
        )
        for step in steps:
            stop = step()
            if stop is not None:
                return stop
        self._record_warning_facts()
        contract = self._report_failures()
        return final_verdict(
            ProofEvidence(
                target=self.target,
                tally=self.tally,
                console=self.console,
                failures=self.failures,
                missing_specification=self.details["missing_specification"],
                contract=contract,
                require_termination=self.wp.require_termination,
                provers=list(self.wp.provers),
            )
        )

    def _invoke(self, what: str, tool: str, argv: list[str], limits: Limits) -> ProcResult:
        ctx = self.ctx
        proc = ctx.run(argv, limits=limits, cwd=self.snapshot, env_extra=self.env, mounts=self.mounts)
        log = self.raw_root / f"{what}.log"
        log.write_text(redact(proc.stdout + (STDERR_SEPARATOR + proc.stderr if proc.stderr else "")), encoding="utf-8")
        self.details["raw"][f"{what}_log"] = ctx.relrun(log)
        self.details["argv"][what] = list(argv)
        note = f"output truncated at {limits.max_output_bytes} bytes" if proc.output_truncated else None
        self.invocations.append(proc.to_invocation(tool, ctx.run_dir, ctx.log_name(STAGE, what), note=note))
        return proc

    def _check_spec(self) -> Verdict | None:
        if self.wp is None:
            return Verdict(Outcome.ERROR, "the spec has no 'wp' section")
        if self.spec.language.is_cpp:
            return Verdict(Outcome.ERROR, "Frama-C/WP supports C only; the spec is C++")
        return None

    def _locate_framac(self) -> Verdict | None:
        fc = find_framac()
        if fc is None:
            return Verdict(
                Outcome.ERROR,
                f"frama-c executable not found ({INSTALL_HINT}); deductive verification was not performed",
            )
        self.fc = fc
        version = tool_version(fc.real, "-version")
        self.details["frama_c"] = fc.frama_c
        self.details["frama_c_version"] = version
        if framac_version_key(version)[0] < MIN_FRAMAC_MAJOR:
            return Verdict(
                Outcome.ERROR,
                f"{fc.frama_c} reports version {version or '(unknown)'}; Frama-C >= "
                f"{MIN_FRAMAC_MAJOR} is required ({INSTALL_HINT})",
            )
        return None

    def _check_provers(self) -> Verdict | None:
        fc = self.fc
        self.details["why3"] = fc.why3
        if fc.why3:
            self.details["why3_version"] = tool_version(fc.why3, "--version")
        for prover in self.wp.provers:
            if not is_automatic_prover(prover):
                return Verdict(Outcome.ERROR, f"wp.provers contains {prover!r}, {NON_AUTOMATIC_PROVER_ADVICE}")
            executable = prover_executable(fc, prover)
            if executable:
                self.details["prover_versions"][prover] = tool_version(executable, "--version")
        return None

    def _read_inputs(self) -> Verdict | None:
        sources = self.spec.sources
        missing: list[str] = []
        for header in sources.headers:
            path = self.snapshot / header
            if path.is_file():
                self.headers[header] = path.read_text(encoding="utf-8", errors="replace")
            else:
                missing.append(header)
        missing += [rel for rel in sources.editable if not (self.snapshot / rel).is_file()]
        if missing:
            return Verdict(
                Outcome.ERROR, f"cannot run Frama-C/WP: missing input file(s) in the snapshot: {', '.join(missing)}"
            )
        return None

    def _find_approved_contract(self) -> Verdict | None:
        self.contracts = [c for c in target_contracts(self.target, self.headers) if c.has_obligations]
        self.details["contract"] = [
            {"file": c.file, "line": c.line, "clauses": c.clauses, "behaviors": c.behaviors} for c in self.contracts
        ]
        if self.contracts:
            return None
        return Verdict(
            Outcome.INCONCLUSIVE,
            f"no approved ACSL contract: no prototype of {self.target}() in the protected headers "
            f"({', '.join(self.spec.sources.headers) or 'none'}) carries an ACSL contract with ensures/assigns "
            "clauses, so there is nothing to prove (the contract must be part of the approved spec)",
        )

    def _reject_untrusted_acsl(self) -> Verdict | None:
        untrusted = _untrusted_acsl(self.ctx, self.snapshot, self.headers)
        if not untrusted:
            return None
        self.details["notes"].append(
            "untrusted ACSL in editable sources: " + "; ".join(untrusted[:MAX_NOTED_ACSL_VIOLATIONS])
        )
        return Verdict(
            Outcome.INCONCLUSIVE,
            f"the editable sources contain ACSL that could add unproved assumptions ({excerpt(untrusted)}); WP was "
            "not run because its verdict could not be trusted (the integrity gate reports these violations)",
        )

    def _make_work_dirs(self) -> None:
        for directory in (self.raw_root, self.work, self.work / "session", self.work / "out"):
            directory.mkdir(parents=True, exist_ok=True)

    def _prepare_run(self) -> Verdict | None:
        ctx = self.ctx
        wp = self.wp
        try:
            self._make_work_dirs()
        except OSError as exc:
            return Verdict(Outcome.ERROR, f"cannot prepare the Frama-C run: {exc}")
        problem = shell_unsafe_preprocessor_argument(ctx)
        if problem is not None:
            return Verdict(Outcome.ERROR, f"cannot prepare the Frama-C run: {problem}")
        self.limits = Limits(
            timeout_s=ctx.timeout(wp.timeout_s),
            memory_mb=wp.memory_mb,
            max_output_bytes=MAX_OUTPUT_BYTES,
            max_file_mb=MAX_FILE_MB,
        )
        self.details["prover_timeout_s"] = prover_timeout(ctx)
        flags, extra_checks = rte_flags(ctx)
        self.details["rte_checks"] = list(RTE_CLASSES) + [RTE_EXTRA_CHECK_DESCRIPTIONS[c] for c in extra_checks]
        self.details["rte_flags"] = flags
        self.mounts = ctx.mounts(extra_ro=extra_read_only_mounts(self.fc, list(wp.provers)))
        self.env = {"PATH": tool_path(self.fc), "WHY3CONFIG": str(self.why3_conf)}
        self.deadline = time.monotonic() + self.limits.timeout_s
        return None

    def _detect_why3_provers(self) -> Verdict | None:
        why3 = self.fc.why3
        if not why3:
            self.details["notes"].append(
                "why3 executable not found next to frama-c: WP detected the provers on PATH itself"
            )
            return None
        limits = Limits(timeout_s=min(WHY3_DETECT_TIMEOUT_S, self.limits.timeout_s))
        proc = self._invoke("why3-config", "why3", [why3, "config", "detect"], limits)
        if proc.timed_out or proc.error or proc.exit_code != 0 or not self.why3_conf.is_file():
            reason = proc.error or ("timed out" if proc.timed_out else f"exit {proc.exit_code}")
            return Verdict(
                Outcome.ERROR,
                f"`why3 config detect` failed ({reason}): "
                f"{redact((proc.stderr or proc.stdout).strip())[-MAX_WHY3_ERROR_TAIL:]}",
            )
        conf_text = self.why3_conf.read_text(encoding="utf-8", errors="replace")
        kept_conf = self.raw_root / "why3.conf"
        kept_conf.write_text(conf_text, encoding="utf-8")
        self.details["raw"]["why3_conf"] = self.ctx.relrun(kept_conf)
        self.details["provers_detected"] = why3_config_provers(conf_text)
        self.why3_conf_used = self.why3_conf
        return None

    def _remove_stale_report(self) -> Verdict | None:
        try:
            self.report.unlink(missing_ok=True)
        except OSError as exc:
            return Verdict(Outcome.ERROR, f"cannot remove a stale WP report {self.report}: {exc}")
        return None

    def _run_framac(self) -> Verdict | None:
        argv = build_wp_argv(
            self.ctx, self.fc, report_json=self.report, work_dir=self.work, why3_conf=self.why3_conf_used
        )
        limits = Limits(
            timeout_s=max(1.0, self.deadline - time.monotonic()),
            memory_mb=self.limits.memory_mb,
            max_output_bytes=self.limits.max_output_bytes,
            max_file_mb=self.limits.max_file_mb,
        )
        self.proc = self._invoke("wp", "frama-c", argv, limits)
        return None

    def _console_text(self) -> str:
        return self.proc.stdout + "\n" + self.proc.stderr

    def _check_process(self) -> Verdict | None:
        proc = self.proc
        if proc.error:
            return Verdict(Outcome.ERROR, f"frama-c could not be started: {proc.error} ({INSTALL_HINT})")
        if proc.timed_out:
            return Verdict(
                Outcome.TIMEOUT,
                f"Frama-C/WP timed out after {self.limits.timeout_s:g}s (wp.timeout_s={self.wp.timeout_s:g}, "
                f"timeout scale {self.ctx.options.timeout_scale:g}); no verdict",
            )
        if proc.output_truncated:
            return Verdict(
                Outcome.ERROR, f"Frama-C output exceeded {self.limits.max_output_bytes} bytes and was truncated"
            )
        return None

    def _read_console(self) -> Verdict | None:
        console_text = self._console_text()
        self.console = parse_console(console_text)
        merge = contract_merge_warning(console_text)
        if merge is not None:
            self.details["contract_merge"] = merge[:MAX_MERGE_RECORD_CHARS]
            return _rejected_contract_merge(merge)
        console = self.console
        self.details["console"] = {
            "proved": console.proved,
            "total": console.total,
            "categories": console.categories,
            "smoke": list(console.smoke) if console.smoke else None,
        }
        self.details["warnings"] = console.warnings[:MAX_RECORDED_WARNINGS]
        return None

    def _check_framac_exit(self) -> Verdict | None:
        proc = self.proc
        sig = _terminating_signal(proc)
        if sig is not None:
            hint = f" (possibly out of memory: wp.memory_mb={self.wp.memory_mb})" if sig in OUT_OF_MEMORY_SIGNALS else ""
            return Verdict(Outcome.ERROR, f"Frama-C was killed by {_signal_name(sig)}{hint}")
        if self.console.errors or proc.exit_code != 0:
            shown = excerpt(self.console.errors) or redact(self._console_text().strip())[-MAX_CONSOLE_TAIL:]
            return Verdict(
                Outcome.ERROR,
                f"Frama-C failed (exit code {proc.exit_code}): {shown} - the program or its ACSL annotations could "
                "not be parsed / type-checked, or a prover is not configured; no verdict",
            )
        if self.console.cached:
            return Verdict(
                Outcome.ERROR,
                f"WP reported cached proof results ({excerpt(self.console.cached)}) although the cache "
                "is disabled; a verdict must come from this run",
            )
        return None

    def _load_report(self) -> Verdict | None:
        try:
            self.goals = json.loads(self.report.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if not self.console.no_goals:
                return Verdict(Outcome.ERROR, "Frama-C/WP produced no JSON report (-wp-report-json); no verdict")
            self.goals = []
        except (OSError, ValueError) as exc:
            return Verdict(Outcome.ERROR, f"cannot parse the WP JSON report: {exc}")
        self.details["raw"]["report_json"] = self.ctx.relrun(self.report) if self.report.is_file() else None
        return None

    def _check_report(self) -> Verdict | None:
        goals = self.goals
        if not is_goal_list(goals):
            return Verdict(Outcome.ERROR, "the WP JSON report has an unexpected structure; no verdict")
        cached = [goal["goal"] for goal in goals if goal.get("cached")]
        if cached:
            return Verdict(
                Outcome.ERROR,
                f"WP reported cached results for {len(cached)} goal(s) "
                f"({', '.join(cached[:MAX_LISTED])}); a verdict must come from this run",
            )
        if self.console.total is None:
            if self.console.no_goals or not goals:
                return Verdict(Outcome.INCONCLUSIVE, "WP generated no proof obligation at all; nothing was verified")
            return Verdict(Outcome.ERROR, "the Frama-C output has no 'Proved goals: N / M' summary; no verdict")
        return None

    def _tally_goals(self) -> Verdict | None:
        tally = tally_goals(
            self.goals, target=self.target, snapshot=str(self.snapshot), share=framac_share_dir(self.fc)
        )
        self.tally = tally
        self.details["goals"] = tally.entries
        self.details["goals_by_verdict"] = tally.by_verdict
        self.details["smoke_tests"] = {"passed": tally.smoke_passed, "total": tally.smoke_total}
        self.details["goals_total"] = self.console.total
        self.details["goals_proved"] = self.console.proved
        return None

    def _check_goal_consistency(self) -> Verdict | None:
        console = self.console
        tally = self.tally
        discharged = console.goals_discharged_without_prover()
        if len(tally.entries) + discharged != console.total:
            return Verdict(
                Outcome.ERROR,
                f"inconsistent WP output: the JSON report lists {len(tally.entries)} goal(s) (+{discharged} "
                f"discharged without a prover) but the console reports {console.total}; no verdict",
            )
        if console.smoke is not None and console.smoke != (tally.smoke_passed, tally.smoke_total):
            return Verdict(
                Outcome.ERROR,
                f"inconsistent WP output: smoke tests {console.smoke[0]}/{console.smoke[1]} on the "
                f"console vs {tally.smoke_passed}/{tally.smoke_total} in the JSON report; no verdict",
            )
        if tally.prover_failed:
            names = ", ".join(str(entry["goal"]) for entry in tally.prover_failed[:MAX_LISTED])
            return Verdict(
                Outcome.ERROR,
                f"prover failure on {len(tally.prover_failed)} goal(s) ({names}): the prover crashed "
                f"or is not usable ({', '.join(self.wp.provers)}); no verdict",
            )
        return None

    def _record_warning_facts(self) -> None:
        facts = warning_facts(self.console.warnings)
        self.details["memory_model_hypotheses"] = facts.memory_model_hypotheses
        self.details["missing_specification"] = facts.missing_specification
        self.details["library_functions_with_generated_clauses"] = facts.generated_clauses

    def _report_failures(self) -> ContractApplication:
        lines = SourceLines(self.snapshot)
        report_ref = self.details["raw"].get("report_json")
        tried = ", ".join(self.wp.provers)
        tally = self.tally
        self.failures += [refuted_goal_failure(entry, lines, report_ref) for entry in tally.invalid]
        self.failures += [
            unproved_goal_failure(entry, lines, report_ref, tried, self.details["prover_timeout_s"])
            for entry in tally.unproved
        ]
        self.failures += [smoke_test_failure(entry, lines, self.target, report_ref) for entry in tally.smoke_failed]
        self.diagnostics += [failure_diagnostic(failure) for failure in self.failures]
        contract = contract_application(self.contracts, tally.target_goals)
        if not contract.applied:
            self.failures.append(
                contract_not_applied_failure(self.contracts, self.target, contract.missing_clause, report_ref)
            )
        return contract


def run_wp(ctx: BackendContext) -> StageResult:
    run = _WpRun(ctx)
    return run.result(run.decide())
