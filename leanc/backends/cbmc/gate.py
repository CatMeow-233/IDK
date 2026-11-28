from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from leanc.backends.cbmc.argv import Mode, compile_translation
from leanc.backends.cbmc.assumptions import scope_assumptions
from leanc.backends.cbmc.bodiless import (
    UNDECLARED_CONSEQUENCE,
    DependentViolation,
    ViolationSplit,
    bodiless_function,
    call_list,
    is_modelled_bodiless,
    no_body_property_function,
    split_violations,
    undecided_explained_by_unmodelled,
    undeclared_function,
    undeclared_reason,
    unmodelled_reason,
)
from leanc.backends.cbmc.compile_flags import (
    CompileFlagTranslation,
    platform_architecture,
)
from leanc.backends.cbmc.coverage import coverage_by_file
from leanc.backends.cbmc.execution import (
    MAX_LISTED_ITEMS,
    STAGE,
    TOOL,
    CbmcRun,
    ToolProblem,
    cbmc_limits,
    error_diagnostics,
    execute_cbmc,
    messages_excerpt,
    ran_out_of_memory,
    tool_problem,
)
from leanc.backends.cbmc.harness import count_harness_assertions
from leanc.backends.cbmc.output import program_version
from leanc.backends.cbmc.program_inputs import ProgramInputs, cprover_declaration_problem, prepare_inputs
from leanc.backends.cbmc.properties import (
    PropertyTally,
    count_listed_harness_assertions,
    tally_properties,
)
from leanc.backends.cbmc.vacuity_stage import check_vacuity, vacuity_run_wanted
from leanc.backends.counterexample.locations import location_from_cbmc
from leanc.backends.counterexample.properties import property_class
from leanc.backends.counterexample.reports import (
    failure_from_cbmc_property,
    unwind_failure,
)
from leanc.foundation.models import (
    Diagnostic,
    FailureReport,
    Location,
    StageResult,
    ToolInvocation,
)
from leanc.foundation.runner import Limits
from leanc.foundation.status import Outcome, outcome_to_status
from leanc.toolchain.context import BackendContext

MAX_RECORDED_WARNINGS = 50
MAX_SUMMARY_INPUTS = 6
VERDICT_EXIT_CODES = (0, 10)
PARAMETER_PREFIX = "param "

CBMC_NOT_FOUND = (
    "cbmc executable not found (install CBMC, e.g. `apt-get install cbmc`, or set LEANC_CBMC); "
    "formal verification was not performed"
)
NOTHING_CHECKED = "CBMC generated no property to check for this harness and code; nothing was verified"
UNMODELLED_RULE = "unmodelled-callee"


def _initial_details(ctx: BackendContext, compile_options: CompileFlagTranslation) -> dict[str, Any]:
    verification = ctx.spec.verification
    return {
        "cbmc": ctx.tools.cbmc,
        "cbmc_version": ctx.tools.cbmc_version,
        "sandbox": ctx.sandbox.kind,
        "argv": {},
        "raw": {},
        "checks": ctx.spec.effective_cbmc_checks(),
        "unwind": verification.unwind,
        "unwindset": dict(verification.unwindset),
        "malloc_may_fail": verification.malloc_may_fail,
        "object_bits": verification.object_bits,
        "properties_total": 0,
        "properties_passed": 0,
        "properties_failed": 0,
        "properties_unresolved": 0,
        "properties_unmodelled": 0,
        "unwind_failures": [],
        "unmodelled_violations": [],
        "bodiless_functions": {"modelled": [], "unmodelled": []},
        "undeclared_functions": [],
        "compile_flags": compile_options.details(),
        "vacuity": {"status": "skipped", "probes": {}},
        "coverage": None,
        "cbmc_warnings": [],
        "notes": [],
        "assumptions": [],
    }


@dataclass
class _Gate:
    ctx: BackendContext
    limits: Limits
    compile_options: CompileFlagTranslation
    details: dict[str, Any]
    started: float
    invocations: list[ToolInvocation] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    failures: list[FailureReport] = field(default_factory=list)
    platform: str | None = None
    inputs: ProgramInputs | None = None
    bodiless_functions: list[str] = field(default_factory=list)
    undeclared_functions: list[str] = field(default_factory=list)

    @classmethod
    def start(cls, ctx: BackendContext) -> _Gate:
        started = time.monotonic()
        compile_options = compile_translation(ctx.spec)
        gate = cls(
            ctx=ctx,
            limits=cbmc_limits(ctx),
            compile_options=compile_options,
            details=_initial_details(ctx, compile_options),
            started=started,
        )
        if compile_options.dropped_defines:
            gate.note(
                f"compile define(s) {list(compile_options.dropped_defines)} not passed to CBMC: "
                "NDEBUG would disable the harness's assert() checks"
            )
        return gate

    def note(self, text: str) -> None:
        self.details["notes"].append(text)

    def unmodelled_functions(self) -> list[str]:
        return [function for function in self.bodiless_functions if not is_modelled_bodiless(function)]

    def finish(self, outcome: Outcome, summary: str) -> StageResult:
        unapplied = self.compile_options.not_applied_on(platform_architecture(self.platform))
        self.details["compile_flags"]["not_applied"] = unapplied
        self.details["bodiless_functions"] = {
            "modelled": [function for function in self.bodiless_functions if is_modelled_bodiless(function)],
            "unmodelled": self.unmodelled_functions(),
        }
        self.details["undeclared_functions"] = list(self.undeclared_functions)
        self.details["assumptions"] = scope_assumptions(
            self.ctx,
            version=self.details.get("cbmc_version"),
            platform=self.platform,
            vacuity=self.details.get("vacuity"),
            bodiless=self.details["bodiless_functions"],
            compile_options=self.compile_options,
            unapplied_flags=unapplied,
        )
        return StageResult(
            stage=STAGE,
            outcome=outcome,
            status=outcome_to_status(STAGE, outcome),
            summary=summary,
            diagnostics=self.diagnostics,
            failures=self.failures,
            invocations=self.invocations,
            details=self.details,
            duration_s=round(time.monotonic() - self.started, 3),
        )

    def fail_with(self, problem: ToolProblem, summary: str | None = None) -> StageResult:
        return self.finish(problem.outcome, problem.summary if summary is None else summary)

    def stopped(self, problem: ToolProblem) -> StageResult:
        self.diagnostics.extend(problem.diagnostics)
        return self.fail_with(problem)

    def execute(self, mode: Mode) -> CbmcRun:
        if self.inputs is None:
            raise ValueError("the CBMC translation units were not prepared")
        run = execute_cbmc(self.ctx, mode, self.limits, self.inputs.for_mode(mode))
        self.invocations.append(run.invocation)
        self.details["argv"][mode] = run.argv
        self.details["raw"][mode] = run.raw_ref
        if not self.details.get("cbmc_version"):
            self.details["cbmc_version"] = program_version(run.output.program)
        self.platform = self.platform or run.output.platform
        return run

    def execute_checked(self, mode: Mode) -> tuple[CbmcRun, ToolProblem | None]:
        run = self.execute(mode)
        return run, tool_problem(run, self.limits, self.ctx, expect="goals" if mode == "coverage" else "result")

    def record_no_body_properties(self, results: list[dict]) -> set[str]:
        callers: set[str] = set()
        for result in results:
            function = no_body_property_function(result)
            if function is None:
                continue
            if function not in self.bodiless_functions:
                self.bodiless_functions.append(function)
                self.diagnostics.append(_no_body_diagnostic(function, result))
            location = result.get("sourceLocation")
            if isinstance(location, dict) and isinstance(location.get("function"), str):
                callers.add(location["function"])
        return callers

    def record_warnings(self, run: CbmcRun) -> None:
        for warning in run.output.warnings:
            text = str(warning.get("messageText", "")).strip()
            if len(self.details["cbmc_warnings"]) < MAX_RECORDED_WARNINGS:
                self.details["cbmc_warnings"].append(text)
            function = bodiless_function(text)
            if function is not None and function not in self.bodiless_functions:
                self.bodiless_functions.append(function)
                self.diagnostics.append(_no_body_diagnostic(function, warning))
            undeclared = undeclared_function(text)
            if undeclared is not None and undeclared not in self.undeclared_functions:
                self.undeclared_functions.append(undeclared)
                self.diagnostics.append(_undeclared_diagnostic(undeclared, warning))


def _no_body_diagnostic(function: str, warning: dict) -> Diagnostic:
    location = location_from_cbmc(warning.get("sourceLocation"))
    if is_modelled_bodiless(function):
        return Diagnostic(
            tool=TOOL,
            rule_id="no-body",
            severity="note",
            message=f"no body for function {function}: modelled by CBMC as a built-in or nondeterministic input",
            location=location,
        )
    return Diagnostic(
        tool=TOOL,
        rule_id="no-body",
        severity="warning",
        message=(
            f"no body for function {function}: CBMC does not model it (arbitrary return value, writes through its "
            "pointer arguments dropped, its memory accesses unchecked); the CBMC result cannot be a verified verdict"
        ),
        location=location,
    )


def _undeclared_diagnostic(function: str, warning: dict) -> Diagnostic:
    return Diagnostic(
        tool=TOOL,
        rule_id="undeclared-function",
        severity="warning",
        message=f"function {function} is not declared: " + UNDECLARED_CONSEQUENCE,
        location=location_from_cbmc(warning.get("sourceLocation")),
    )


def _record_dependent_violations(gate: _Gate, dependent: list[DependentViolation]) -> None:
    gate.details["properties_unmodelled"] = len(dependent)
    for item in dependent:
        location = location_from_cbmc(item.result.get("sourceLocation"))
        property_id = item.result.get("property")
        description = item.result.get("description")
        gate.details["unmodelled_violations"].append(
            {
                "property": property_id,
                "description": description,
                "location": location.short() if location else None,
                "functions": list(item.functions),
            }
        )
        gate.diagnostics.append(
            Diagnostic(
                tool=TOOL,
                rule_id=UNMODELLED_RULE,
                severity="warning",
                message=(
                    f"{description} [{property_id}] is not a counterexample: its trace passes through "
                    f"{call_list(item.functions)}, which CBMC does not model (no body)"
                ),
                location=location,
            )
        )


def _missing_inputs(ctx: BackendContext) -> list[str]:
    inputs = [*ctx.spec.sources.editable, ctx.spec.verification.harness]
    return [path for path in inputs if not (ctx.snapshot_dir / path).is_file()]


def _reject_neutralised_harness_assertions(gate: _Gate, results: list[dict], harness_text: str) -> StageResult | None:
    harness = gate.ctx.spec.verification.harness
    in_source = count_harness_assertions(harness_text)
    listed = count_listed_harness_assertions(results, harness)
    gate.details["harness_assertions"] = {"in_source": in_source, "cbmc_properties": listed}
    if listed >= in_source:
        return None
    gate.diagnostics.append(
        Diagnostic(
            tool=TOOL,
            rule_id="missing-assertions",
            severity="error",
            message=(
                f"the harness main() contains {in_source} assertion(s) but CBMC lists only "
                f"{listed} assertion property(ies) for it"
            ),
            location=Location(file=harness, function="main"),
        )
    )
    return gate.finish(
        Outcome.ERROR,
        "harness assertions missing from CBMC's property list (assert machinery overridden?): "
        f"{harness} main() contains {in_source} assertion(s), CBMC checked {listed}; "
        "no verdict is possible",
    )


def _record_tally(gate: _Gate, tally: PropertyTally, verify_run: CbmcRun) -> None:
    gate.details["properties_total"] = tally.checked
    gate.details["properties_passed"] = tally.holding
    gate.details["properties_unresolved"] = len(tally.undecided)
    gate.details["unwinding_assertions_total"] = tally.unwinding_total
    gate.details["cprover_status"] = verify_run.output.cprover_status
    if verify_run.proc.exit_code not in VERDICT_EXIT_CODES:
        gate.note(f"unexpected CBMC exit code {verify_run.proc.exit_code} for the verify run")


def _target_without_body(gate: _Gate, tally: PropertyTally) -> StageResult:
    target = gate.ctx.spec.target.function
    gate.details["properties_unmodelled"] = len(tally.violated)
    gate.note(
        f"{len(tally.violated)} reported violation(s) discarded: they depend on arbitrary return values of the "
        f"undefined {target}()"
    )
    return gate.finish(
        Outcome.INCONCLUSIVE,
        f"the function under verification {target}() has no definition in the editable sources; "
        "CBMC treats it as returning arbitrary values, so no verdict is possible",
    )


def _report_counterexamples(gate: _Gate, violated: list[dict], verify_run: CbmcRun) -> list[FailureReport]:
    spec = gate.ctx.spec
    gate.details["properties_failed"] = len(violated)
    reports = []
    for result in violated:
        failure = failure_from_cbmc_property(
            result, raw_ref=verify_run.raw_ref, harness=spec.verification.harness, target_function=spec.target.function
        )
        reports.append(failure)
        gate.diagnostics.append(
            Diagnostic(
                tool=TOOL,
                rule_id=property_class(failure.property_id, failure.description),
                severity="error",
                message=f"{failure.description} [{failure.property_id}]",
                location=failure.location,
            )
        )
    gate.failures.extend(reports)
    return reports


def _report_unwind_failures(gate: _Gate, tally: PropertyTally, verify_run: CbmcRun) -> list[FailureReport]:
    spec = gate.ctx.spec
    unwind = spec.verification.unwind
    reports = []
    for result in tally.unwinding_failed:
        failure = unwind_failure(
            result,
            raw_ref=verify_run.raw_ref,
            unwind=unwind,
            harness=spec.verification.harness,
            target_function=spec.target.function,
        )
        reports.append(failure)
        gate.details["unwind_failures"].append(
            {
                "property": failure.property_id,
                "description": failure.description,
                "location": failure.location.short() if failure.location else None,
            }
        )
        gate.diagnostics.append(
            Diagnostic(
                tool=TOOL,
                rule_id="unwind",
                severity="warning",
                message=f"{failure.description} [{failure.property_id}]: unwind bound {unwind} is insufficient",
                location=failure.location,
            )
        )
    gate.failures.extend(reports)
    return reports


def _located(failure: FailureReport) -> str:
    return f" at {failure.location.short()}" if failure.location else ""


def _insufficient_unwind_text(reports: list[FailureReport], unwind: int) -> str:
    if not reports:
        return ""
    loops = ", ".join(f"{failure.property_id}{_located(failure)}" for failure in reports[:MAX_LISTED_ITEMS])
    return (
        f"{len(reports)} unwinding assertion(s) failed ({loops}): the unwind bound {unwind} is "
        "insufficient, so executions needing more iterations were not explored and the result is not sound "
        "for the input space; raising the bound requires a re-approved spec"
    )


def _counterexample_summary(failure: FailureReport) -> str:
    summary = f"{failure.property_id} ({failure.description}){_located(failure)}"
    parameters = [item for item in failure.inputs if item.name.startswith(PARAMETER_PREFIX)] or failure.inputs
    if parameters:
        summary += " with " + ", ".join(
            f"{item.name.removeprefix(PARAMETER_PREFIX)}={item.value}" for item in parameters[:MAX_SUMMARY_INPUTS]
        )
    return summary


@dataclass(frozen=True)
class _ViolationFindings:
    counterexamples: list[FailureReport]
    split: ViolationSplit
    unwind_text: str


def _counterexample_verdict(
    gate: _Gate, findings: _ViolationFindings, tally: PropertyTally, verify_run: CbmcRun
) -> StageResult:
    counterexamples, unwind_text = findings.counterexamples, findings.unwind_text
    gate.details["vacuity"] = {"status": "skipped", "probes": {}, "note": "not run: real property violations found"}
    summary = f"CBMC found {len(counterexamples)} property violation(s) (counterexamples): " + "; ".join(
        _counterexample_summary(failure) for failure in counterexamples[:MAX_LISTED_ITEMS]
    )
    if len(counterexamples) > MAX_LISTED_ITEMS:
        summary += f"; and {len(counterexamples) - MAX_LISTED_ITEMS} more"
    if unwind_text:
        summary += f". In addition, {unwind_text}"
    summary += "".join(f". In addition, {reason}" for reason in _model_gap_reasons(gate, findings.split))
    if tally.undecided:
        summary += f". {len(tally.undecided)} property(ies) could not be decided"
        if ran_out_of_memory(verify_run):
            summary += f" (CBMC ran out of memory: verification.memory_mb={gate.ctx.spec.verification.memory_mb})"
    return gate.finish(Outcome.FAIL, summary)


def _undecided_verdict(gate: _Gate, tally: PropertyTally, verify_run: CbmcRun) -> StageResult:
    gate.details["vacuity"] = {"status": "skipped", "probes": {}, "note": "not run: undecided properties"}
    names = ", ".join(f"{result.get('property')} ({result.get('status')})" for result in tally.undecided[:MAX_LISTED_ITEMS])
    excerpt = messages_excerpt(verify_run.output, verify_run.proc.stderr)
    out_of_memory = ran_out_of_memory(verify_run)
    gate.diagnostics.extend(error_diagnostics(verify_run.output, "out-of-memory" if out_of_memory else "cbmc-error"))
    memory_mb = gate.ctx.spec.verification.memory_mb
    head = f"CBMC ran out of memory (verification.memory_mb={memory_mb}) and " if out_of_memory else "CBMC "
    return gate.finish(
        Outcome.ERROR,
        f"{head}could not decide {len(tally.undecided)} property(ies): {names}" + (f"; {excerpt}" if excerpt else ""),
    )


def _record_informational_coverage(gate: _Gate) -> None:
    coverage_run, problem = gate.execute_checked("coverage")
    if problem is not None:
        gate.details["coverage_error"] = problem.summary
        gate.note(f"coverage run failed (non-blocking): {problem.summary}")
        return
    coverage = coverage_by_file(coverage_run.output.goals or [], list(gate.ctx.spec.sources.editable))
    gate.details["coverage"] = coverage
    for file, stats in coverage.items():
        never_reached = stats["uncovered_lines"]
        gate.note(
            f"coverage (non-blocking) {file}: {stats['covered']}/{stats['goals']} basic block(s) reachable by the harness"
            + (f"; never reached: lines {', '.join(map(str, never_reached))}" if never_reached else "")
        )


def _model_gap_reasons(gate: _Gate, split: ViolationSplit) -> list[str]:
    unmodelled = gate.unmodelled_functions()
    reasons = [unmodelled_reason(unmodelled, split.dependent)] if unmodelled else []
    if gate.undeclared_functions:
        reasons.append(undeclared_reason(gate.undeclared_functions))
    return reasons


def _pass_summary(gate: _Gate, tally: PropertyTally) -> str:
    verification = gate.ctx.spec.verification
    probe_count = len(gate.details["vacuity"].get("properties", {}))
    return (
        f"CBMC: all {tally.checked} properties hold for the approved harness within unwind {verification.unwind} "
        f"(all loops fully unwound; all {probe_count} vacuity probe(s) reachable; "
        f"checks: {', '.join(gate.ctx.spec.effective_cbmc_checks())})"
    )


def run_cbmc(ctx: BackendContext) -> StageResult:
    gate = _Gate.start(ctx)
    verification = ctx.spec.verification
    if not ctx.tools.cbmc:
        return gate.finish(Outcome.ERROR, CBMC_NOT_FOUND)
    missing = _missing_inputs(ctx)
    if missing:
        return gate.finish(Outcome.ERROR, f"cannot run CBMC: missing input file(s) in the snapshot: {', '.join(missing)}")
    harness_text = (ctx.snapshot_dir / verification.harness).read_text(encoding="utf-8", errors="replace")
    inputs = prepare_inputs(ctx, gate.limits, gate.invocations, with_vacuity=vacuity_run_wanted(verification, harness_text))
    if isinstance(inputs, ToolProblem):
        return gate.stopped(inputs)
    gate.inputs = inputs
    verify_run, problem = gate.execute_checked("verify")
    if problem:
        return gate.stopped(problem)
    declared = cprover_declaration_problem(ctx, gate.limits, inputs, gate.invocations)
    if declared is not None:
        return gate.stopped(declared)
    gate.record_warnings(verify_run)
    results = verify_run.output.results or []
    no_body_callers = gate.record_no_body_properties(results)
    rejected = _reject_neutralised_harness_assertions(gate, results, harness_text)
    if rejected is not None:
        return rejected
    tally = tally_properties(results)
    _record_tally(gate, tally, verify_run)
    if ctx.spec.target.function in gate.bodiless_functions:
        return _target_without_body(gate, tally)

    unmodelled_set = frozenset(gate.unmodelled_functions())
    split = split_violations(tally.violated, unmodelled_set)
    _record_dependent_violations(gate, split.dependent)
    counterexamples = _report_counterexamples(gate, split.real, verify_run)
    unwind_reports = _report_unwind_failures(gate, tally, verify_run)
    unwind_text = _insufficient_unwind_text(unwind_reports, verification.unwind)
    if counterexamples:
        return _counterexample_verdict(gate, _ViolationFindings(counterexamples, split, unwind_text), tally, verify_run)
    if tally.undecided:
        unexplained = ran_out_of_memory(verify_run) or any(
            not undecided_explained_by_unmodelled(item, unmodelled_set, no_body_callers) for item in tally.undecided
        )
        if unexplained:
            return _undecided_verdict(gate, tally, verify_run)

    vacuity_problem, vacuity_reasons = check_vacuity(gate, harness_text, unwind_insufficient=bool(unwind_reports))
    inconclusive = _model_gap_reasons(gate, split) + ([unwind_text] if unwind_text else []) + vacuity_reasons
    if vacuity_problem is not None:
        also = ". Also: " + "; ".join(inconclusive) if inconclusive else ""
        return gate.fail_with(vacuity_problem, vacuity_problem.summary + also)
    if tally.checked == 0 and not inconclusive:
        inconclusive.append(NOTHING_CHECKED)
    if inconclusive:
        return gate.finish(Outcome.INCONCLUSIVE, "CBMC verdict inconclusive: " + "; ".join(inconclusive))
    if verification.coverage:
        _record_informational_coverage(gate)
    return gate.finish(Outcome.PASS, _pass_summary(gate, tally))
