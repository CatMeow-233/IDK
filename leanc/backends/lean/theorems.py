from __future__ import annotations

from dataclasses import dataclass, field

from leanc.backends.lean.checker import CheckerStatus
from leanc.backends.lean.steps import GateAbort
from leanc.foundation.models import FailureReport
from leanc.foundation.status import Outcome
from leanc.specs.model import LeanConfig

STANDARD_AXIOMS: tuple[str, ...] = ("propext", "Classical.choice", "Quot.sound")
SORRY_AXIOM = "sorryAx"
UNDEFINED_PROOF_MARKER = "not defined"
INVALID_STATEMENT = "invalid statement"
MAX_LISTED_MISSING_CONSTANTS = 5


@dataclass
class TheoremResult:
    name: str
    statement: str
    proof: str
    proved: bool = False
    axioms: list[str] = field(default_factory=list)
    reason: str = ""
    spec_error: str | None = None

    def as_dict(self) -> dict:
        record = {
            "name": self.name,
            "statement": self.statement,
            "proof": self.proof,
            "proved": self.proved,
            "axioms": list(self.axioms),
        }
        if self.reason:
            record["reason"] = self.reason
        if self.spec_error:
            record["spec_error"] = self.spec_error
        return record


def _settle_checked(theorem: TheoremResult, axioms: list[str], missing: list[str]) -> None:
    nonstandard = sorted(axiom for axiom in axioms if axiom not in STANDARD_AXIOMS)
    if missing:
        theorem.reason = (
            f"the proof refers to constants missing from the environment: {missing[:MAX_LISTED_MISSING_CONSTANTS]}"
        )
    elif nonstandard:
        theorem.reason = (
            f"uses `sorry` ({SORRY_AXIOM})" if nonstandard == [SORRY_AXIOM] else f"depends on non-standard axioms {nonstandard}"
        )
    else:
        theorem.proved = True


def _settle_unproved(theorem: TheoremResult, detail: str, failed_files: dict[str, str]) -> None:
    theorem.reason = detail
    if UNDEFINED_PROOF_MARKER in detail and failed_files:
        theorem.reason += " (" + "; ".join(f"{rel}: {state}" for rel, state in failed_files.items()) + ")"


def settle_theorem(theorem: TheoremResult, report: dict, failed_files: dict[str, str]) -> None:
    status = report.get("status")
    detail = str(report.get("detail") or "")
    axioms = [str(axiom) for axiom in report.get("axioms") or []]
    missing = [str(constant) for constant in report.get("missing") or []]
    theorem.axioms = sorted(axioms)
    if status == CheckerStatus.SPEC_ERROR:
        theorem.spec_error = detail or INVALID_STATEMENT
    elif status == CheckerStatus.CHECKED:
        _settle_checked(theorem, axioms, missing)
    elif status == CheckerStatus.UNPROVED:
        _settle_unproved(theorem, detail, failed_files)
    else:
        raise GateAbort(Outcome.ERROR, f"the trusted checker returned an unknown status {status!r}")


def unproved_theorem_failure(theorem: TheoremResult, lean: LeanConfig, checker_ref: str) -> FailureReport:
    return FailureReport(
        id=f"lean:unproved:{theorem.name}",
        kind="unproved",
        tool="lean",
        property_id=theorem.name,
        description=f"Lean theorem '{theorem.name}' ({theorem.statement}) is not proved by {theorem.proof}\n"
        f"reason: {theorem.reason}",
        explanation=(
            f"Define `{theorem.proof}` in an editable proof file ({', '.join(lean.proofs)}) as a theorem whose type "
            f"is exactly `{theorem.statement}` from {lean.spec_file}, proved with core Lean 4 tactics only (omega, "
            "simp, decide, induction, ...): no sorry/admit, no axioms, no native_decide."
        ),
        raw_ref=checker_ref,
    )
