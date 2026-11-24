from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from leanc.backends.cbmc.properties import tally_properties
from leanc.backends.inductive.build import InductiveBuild, build_inductive
from leanc.backends.inductive.checks import (
    harness_assertion_counts,
    harness_rule_problem,
    missing_assertions_problem,
    nothing_checked_problem,
    property_class_counts,
    undecided_problem,
    unmodelled_functions,
)
from leanc.backends.inductive.evidence import EvidencedLoop, loop_evidence
from leanc.backends.inductive.findings import (
    Finding,
    callee_findings,
    failures,
    not_holding,
    property_findings,
    reason_codes,
)
from leanc.backends.inductive.probes import expected_probes, judge_probes
from leanc.backends.inductive.problems import STAGE, BuildProblem
from leanc.backends.inductive.program import BuildMode
from leanc.backends.inductive.runs import INDUCTIVE_CHECKS, CheckedRun, checked_run, inductive_unwind
from leanc.backends.inductive.scope import ScopeInputs, no_claim_statements, scope_details, scope_statements
from leanc.backends.inductive.steps import ToolSteps
from leanc.foundation.inductive_scope import SUBOBJECT_GAP_SHORT
from leanc.foundation.models import Diagnostic, StageResult, ToolInvocation
from leanc.foundation.reasons import STAGE_REASON_CODES_DETAIL, Reason
from leanc.foundation.status import Outcome, outcome_to_status
from leanc.toolchain.context import BackendContext

TOOL = "cbmc"
GENERIC_REASONS = {Outcome.ERROR: Reason.TOOL_ERROR, Outcome.TIMEOUT: Reason.GATE_TIMEOUT}
MAX_SUMMARY_DETAILS = 2
INCONCLUSIVE_HEAD = "inductive verdict inconclusive (no VERIFIED_INDUCTIVE): "
ALSO = ". Also found before the gate stopped: "
PASS_GAPS = (
    "known gaps: harness code other than LEANC_ASSUME narrows the covered states without being listed (review the "
    f"whole harness), {SUBOBJECT_GAP_SHORT}, and library string functions with loops fail closed"
)


@dataclass
class _Runs:
    unwind: int = 0
    version: str | None = None
    platform: str | None = None
    loops: dict[str, EvidencedLoop] = field(default_factory=dict)
    property_classes: dict[str, int] = field(default_factory=dict)


@dataclass
class _Gate:
    ctx: BackendContext
    started: float
    build: InductiveBuild = field(default_factory=InductiveBuild)
    details: dict[str, Any] = field(default_factory=dict)
    invocations: list[ToolInvocation] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    runs: _Runs = field(default_factory=_Runs)

    def absorb_build(self, build: InductiveBuild) -> None:
        self.build = build
        self.invocations.extend(build.invocations)
        self.details.update(build.details())
        self.details.update(
            {
                "sandbox": self.ctx.sandbox.kind,
                "functions": list(self.ctx.spec.inductive.functions) if self.ctx.spec.inductive else [],
                "harness_assumptions": list(build.harness_assumptions),
                "check_classes": list(INDUCTIVE_CHECKS),
                "raw": {},
                "notes": [],
            }
        )

    def record(self, mode: BuildMode, checked: CheckedRun, steps: ToolSteps) -> None:
        self.invocations.append(checked.run.invocation)
        self.details["argv"].update({label: argv for label, argv in steps.argv.items() if label not in self.details["argv"]})
        self.details["raw"][mode.value] = checked.run.raw_ref
        self.diagnostics.extend(checked.diagnostics)
        self.runs.version = self.runs.version or checked.version
        self.runs.platform = self.runs.platform or checked.run.output.platform

    def finish(self, outcome: Outcome, summary: str) -> StageResult:
        self.details[STAGE_REASON_CODES_DETAIL] = [] if outcome is Outcome.PASS else reason_codes(self.findings)
        self.details["cbmc_version"] = self.runs.version
        self.details["unwind"] = self.runs.unwind or None
        inputs = ScopeInputs(self.runs.unwind, self.runs.version, self.runs.platform, self.runs.loops)
        if self.ctx.spec.inductive is not None:
            self.details["scope"] = scope_details(self.ctx, self.build, inputs, self.runs.property_classes)
        if outcome is Outcome.PASS:
            self.details["assumptions"] = scope_statements(self.ctx, self.build, inputs)
        else:
            self.details["assumptions"] = no_claim_statements(outcome)
        return StageResult(
            stage=STAGE,
            outcome=outcome,
            status=outcome_to_status(STAGE, outcome),
            summary=summary,
            diagnostics=self.diagnostics,
            failures=failures(self.findings),
            invocations=self.invocations,
            details=self.details,
            duration_s=round(time.monotonic() - self.started, 3),
        )

    def stop(self, problem: BuildProblem) -> StageResult:
        earlier = list(self.findings)
        if GENERIC_REASONS.get(problem.outcome) is not problem.reason:
            self.findings.insert(0, Finding(problem.reason, problem.describe(), problem.location))
        summary = problem.describe() + (ALSO + findings_summary(earlier) if earlier else "")
        return self.finish(problem.outcome, summary)

    def harness_text(self) -> str:
        harness = self.ctx.spec.inductive.harness
        return (self.ctx.snapshot_dir / harness).read_text(encoding="utf-8", errors="replace")

    def verify(self, steps: ToolSteps) -> BuildProblem | None:
        harness = self.ctx.spec.inductive.harness
        checked = checked_run(steps, BuildMode.VERIFY, self.build.verify.instrumented, self.runs.unwind)
        self.record(BuildMode.VERIFY, checked, steps)
        if checked.problem is not None:
            return checked.problem
        results = checked.results
        tally = tally_properties(results)
        self.runs.property_classes = property_class_counts(results)
        in_source, listed = harness_assertion_counts(results, self.harness_text(), harness)
        self.details["properties"] = {
            "total": tally.checked,
            "holding": tally.holding,
            "not_holding": len(not_holding(results)),
            "undecided": len(tally.undecided),
            "unwinding_assertions": tally.unwinding_total,
        }
        self.details["harness_assertions"] = {"in_source": in_source, "cbmc_properties": listed}
        for problem in (
            undecided_problem(tally),
            harness_rule_problem(results),
            nothing_checked_problem(tally),
            missing_assertions_problem(in_source, listed, harness),
        ):
            if problem is not None:
                return problem
        evidence = loop_evidence(results, self.build.contracts, harness)
        if evidence.problem is not None and evidence.problem.outcome is Outcome.ERROR:
            return evidence.problem
        self.runs.loops = dict(evidence.loops)
        if evidence.problem is not None:
            self.findings.append(Finding(evidence.problem.reason, evidence.problem.describe()))
        unmodelled = unmodelled_functions(checked.run.output, self.build.bodiless)
        self.details["unmodelled_bodiless"] = unmodelled
        self.findings += property_findings(not_holding(results), frozenset(unmodelled), checked.run.raw_ref)
        self.findings += callee_findings(unmodelled)
        return None

    def vacuity(self, steps: ToolSteps) -> BuildProblem | None:
        build = self.build
        checked = checked_run(steps, BuildMode.VACUITY, build.vacuity.instrumented, self.runs.unwind)
        self.record(BuildMode.VACUITY, checked, steps)
        if checked.problem is not None:
            return checked.problem
        unwind_cut = bool(tally_properties(checked.results).unwinding_failed)
        expected = expected_probes(build.entries, build.contracts, build.claimed_calls)
        judgement = judge_probes(
            checked.run, self.harness_text(), self.ctx.spec.inductive.harness, expected, unwind_cut, build.reach_sites
        )
        self.details["vacuity"] = judgement.details
        self.details["notes"].extend(judgement.notes)
        if judgement.problem is not None:
            return judgement.problem
        self.findings += judgement.findings
        return None

    def verdict(self) -> StageResult:
        if self.findings:
            return self.finish(Outcome.INCONCLUSIVE, INCONCLUSIVE_HEAD + findings_summary(self.findings))
        return self.finish(Outcome.PASS, self.pass_summary())

    def pass_summary(self) -> str:
        inductive = self.ctx.spec.inductive
        loops = ", ".join(item.contract.location for item in self.build.contracts) or "none"
        probes = sum(entry["reported"] for entry in self.details["vacuity"].get("inserted", {}).values())
        probes += len(self.details["vacuity"].get("properties", {}))
        return (
            f"inductive: all {self.details['properties']['total']} properties hold on every execution of main() of "
            f"{inductive.harness} as written (within its {len(self.build.harness_assumptions)} harness assumption(s), "
            "narrowed further by its other code), for the claimed "
            f"function(s) {', '.join(f'{name}()' for name in inductive.functions)} with one claimed call per path; "
            f"contracted loops proved by induction: {loops}; checks: {', '.join(INDUCTIVE_CHECKS)} only, plus the "
            f"harness assertions; all {probes} probe(s) reached; {PASS_GAPS}; VERIFIED_INDUCTIVE scope, not PROVED"
        )


def _grouped(findings: Sequence[Finding]) -> dict[Reason, list[Finding]]:
    grouped: dict[Reason, list[Finding]] = {}
    for finding in findings:
        grouped.setdefault(finding.reason, []).append(finding)
    return grouped


def _shown(finding: Finding) -> str:
    where = f" at {finding.location.short()}" if finding.location and finding.location.file else ""
    return finding.detail + where


def findings_summary(findings: Sequence[Finding]) -> str:
    parts = []
    for reason, items in _grouped(findings).items():
        shown = "; ".join(_shown(item) for item in items[:MAX_SUMMARY_DETAILS])
        more = f" (+{len(items) - MAX_SUMMARY_DETAILS} more)" if len(items) > MAX_SUMMARY_DETAILS else ""
        parts.append(f"{reason.value} x{len(items)}: {shown}{more}")
    return " | ".join(parts)


def run_inductive(ctx: BackendContext) -> StageResult:
    gate = _Gate(ctx, time.monotonic())
    build = build_inductive(ctx)
    gate.absorb_build(build)
    if build.problem is not None:
        return gate.stop(build.problem)
    if not build.built:
        raise ValueError("the inductive build reported no problem but produced no programs")
    steps = ToolSteps.start(ctx)
    gate.details["timeout_s_per_run"] = steps.limits.timeout_s
    gate.runs.unwind = inductive_unwind(ctx.spec, build.contracts)
    for step in (gate.verify, gate.vacuity):
        problem = step(steps)
        if problem is not None:
            return gate.stop(problem)
    return gate.verdict()
