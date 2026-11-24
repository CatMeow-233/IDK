from __future__ import annotations

from typing import Any, Protocol

from leanc.backends.cbmc.argv import Mode
from leanc.backends.cbmc.execution import TOOL, CbmcRun, ToolProblem
from leanc.backends.cbmc.harness import find_probes
from leanc.backends.cbmc.program_inputs import ProgramInputs
from leanc.backends.cbmc.vacuity import evaluate_probes
from leanc.backends.cbmc.vacuity_sites import site_failures, unreported_reason, unreported_sites
from leanc.foundation.models import Diagnostic, FailureReport, ToolInvocation
from leanc.foundation.runner import Limits
from leanc.specs.model import VerificationConfig
from leanc.toolchain.context import BackendContext

__all__ = ["UNWIND_CUTOFF_SUFFIX", "VACUITY_DISABLED", "VacuityGate", "check_vacuity", "vacuity_run_wanted"]

VACUITY_DISABLED = "vacuity check disabled in spec; cannot exclude vacuous verification"
UNWIND_CUTOFF_SUFFIX = " (possibly only because executions beyond the unwind bound were cut off)"
VACUITY_RULE_ID = "vacuity"
VACUOUS_STATUS = "vacuous"


class VacuityGate(Protocol):
    ctx: BackendContext
    limits: Limits
    details: dict[str, Any]
    invocations: list[ToolInvocation]
    diagnostics: list[Diagnostic]
    failures: list[FailureReport]
    inputs: ProgramInputs | None

    def execute_checked(self, mode: Mode) -> tuple[CbmcRun, ToolProblem | None]: ...


def vacuity_run_wanted(verification: VerificationConfig, harness_text: str) -> bool:
    has_probes, _ = find_probes(harness_text)
    return verification.vacuity_check and has_probes


def _no_probes_reason(harness: str) -> str:
    return (
        f"no LEANC_REACH probes in the harness {harness}; vacuity cannot be checked "
        '(add LEANC_REACH("label") after the call under verification)'
    )


def _skipped(gate: VacuityGate, note: str, problem: ToolProblem) -> tuple[ToolProblem, list[str]]:
    gate.details["vacuity"] = {"status": "skipped", "probes": {}, "note": f"{note}: {problem.summary}"}
    gate.diagnostics.extend(problem.diagnostics)
    return problem, []


def _record_failures(gate: VacuityGate, failures: list[FailureReport]) -> None:
    gate.failures.extend(failures)
    for failure in failures:
        gate.diagnostics.append(
            Diagnostic(tool=TOOL, rule_id=VACUITY_RULE_ID, severity="warning", message=failure.description, location=failure.location)
        )


def check_vacuity(gate: VacuityGate, harness_text: str, unwind_insufficient: bool) -> tuple[ToolProblem | None, list[str]]:
    verification = gate.ctx.spec.verification
    if not verification.vacuity_check:
        gate.details["vacuity"] = {"status": "disabled", "probes": {}}
        return None, [VACUITY_DISABLED]
    if not vacuity_run_wanted(verification, harness_text):
        gate.details["vacuity"] = {"status": "unchecked", "probes": {}}
        return None, [_no_probes_reason(verification.harness)]
    vacuity_run, problem = gate.execute_checked("vacuity")
    if problem is not None:
        return _skipped(gate, "vacuity run failed", problem)
    verdict = evaluate_probes(vacuity_run, harness_text, harness=verification.harness, unwind_insufficient=unwind_insufficient)
    gate.details["vacuity"] = verdict.details
    gate.details["notes"].extend(verdict.notes)
    if verdict.problem is not None:
        _record_failures(gate, verdict.failures)
        return verdict.problem, []
    sites = gate.inputs.sites if gate.inputs is not None else ()
    unreported = unreported_sites(vacuity_run.output.results or [], sites, verdict.details.get("unreported_labels", []))
    verdict.details["sites"] = len(sites)
    verdict.details["unreported_sites"] = [site.describe() for site in unreported]
    _record_failures(gate, verdict.failures + site_failures(unreported, vacuity_run.raw_ref, unwind_insufficient))
    reasons = [verdict.inconclusive] if verdict.inconclusive else []
    if unreported:
        verdict.details["status"] = VACUOUS_STATUS
        reasons.append(unreported_reason(unreported))
    return None, [reason + (UNWIND_CUTOFF_SUFFIX if unwind_insufficient else "") for reason in reasons]
