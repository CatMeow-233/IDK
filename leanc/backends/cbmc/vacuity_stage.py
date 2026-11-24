from __future__ import annotations

from typing import Any, Protocol

from leanc.backends.cbmc.argv import Mode
from leanc.backends.cbmc.execution import TOOL, CbmcRun, ToolProblem
from leanc.backends.cbmc.harness import find_probes
from leanc.backends.cbmc.vacuity import evaluate_probes
from leanc.backends.cbmc.vacuity_divergence import bounded_divergence_problem
from leanc.foundation.models import Diagnostic, FailureReport, ToolInvocation
from leanc.foundation.runner import Limits
from leanc.toolchain.context import BackendContext

__all__ = ["UNWIND_CUTOFF_SUFFIX", "VACUITY_DISABLED", "VacuityGate", "check_vacuity"]

VACUITY_DISABLED = "vacuity check disabled in spec; cannot exclude vacuous verification"
UNWIND_CUTOFF_SUFFIX = " (possibly only because executions beyond the unwind bound were cut off)"
VACUITY_RULE_ID = "vacuity"


class VacuityGate(Protocol):
    ctx: BackendContext
    limits: Limits
    details: dict[str, Any]
    invocations: list[ToolInvocation]
    diagnostics: list[Diagnostic]
    failures: list[FailureReport]

    def execute_checked(self, mode: Mode) -> tuple[CbmcRun, ToolProblem | None]: ...


def _no_probes_reason(harness: str) -> str:
    return (
        f"no LEANC_REACH probes in the harness {harness}; vacuity cannot be checked "
        '(add LEANC_REACH("label") after the call under verification)'
    )


def _skipped(gate: VacuityGate, note: str, problem: ToolProblem) -> tuple[ToolProblem, list[str]]:
    gate.details["vacuity"] = {"status": "skipped", "probes": {}, "note": f"{note}: {problem.summary}"}
    gate.diagnostics.extend(problem.diagnostics)
    return problem, []


def check_vacuity(gate: VacuityGate, harness_text: str, unwind_insufficient: bool) -> tuple[ToolProblem | None, list[str]]:
    verification = gate.ctx.spec.verification
    if not verification.vacuity_check:
        gate.details["vacuity"] = {"status": "disabled", "probes": {}}
        return None, [VACUITY_DISABLED]
    has_probes, _ = find_probes(harness_text)
    if not has_probes:
        gate.details["vacuity"] = {"status": "unchecked", "probes": {}}
        return None, [_no_probes_reason(verification.harness)]
    divergence = bounded_divergence_problem(gate.ctx, gate.limits, gate.invocations)
    if divergence is not None:
        return _skipped(gate, "vacuity run not started", divergence)
    vacuity_run, problem = gate.execute_checked("vacuity")
    if problem is not None:
        return _skipped(gate, "vacuity run failed", problem)
    verdict = evaluate_probes(vacuity_run, harness_text, harness=verification.harness, unwind_insufficient=unwind_insufficient)
    gate.details["vacuity"] = verdict.details
    gate.details["notes"].extend(verdict.notes)
    gate.failures.extend(verdict.failures)
    for failure in verdict.failures:
        gate.diagnostics.append(
            Diagnostic(tool=TOOL, rule_id=VACUITY_RULE_ID, severity="warning", message=failure.description, location=failure.location)
        )
    if verdict.problem is not None:
        return verdict.problem, []
    if verdict.inconclusive:
        return None, [verdict.inconclusive + (UNWIND_CUTOFF_SUFFIX if unwind_insufficient else "")]
    return None, []
