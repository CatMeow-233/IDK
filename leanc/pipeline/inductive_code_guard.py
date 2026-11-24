from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from leanc.foundation.models import PolicyDecision, PolicyViolation, RepairRound, VerificationResult
from leanc.foundation.status import Outcome
from leanc.repair.patch import changed_paths
from leanc.repair.policy import INDUCTIVE_CODE_EDIT_RULE
from leanc.specs.model import INDUCTIVE_GATE, Spec

__all__ = [
    "BLOCKED_CANDIDATE_DIR",
    "InductiveCodeGuard",
    "blocked_rounds_note",
    "inductive_open",
]

BLOCKED_CANDIDATE_DIR = "blocked-candidate"


def inductive_open(spec: Spec, vr: VerificationResult) -> bool:
    stage = vr.stage(INDUCTIVE_GATE)
    if stage is None:
        return INDUCTIVE_GATE in spec.gates
    return stage.required and stage.outcome is not Outcome.PASS


def _inductive_passes(vr: VerificationResult) -> bool:
    stage = vr.stage(INDUCTIVE_GATE)
    return stage is not None and stage.outcome is Outcome.PASS


def _violation(path: str, status: str) -> PolicyViolation:
    return PolicyViolation(
        rule=INDUCTIVE_CODE_EDIT_RULE,
        message=f"{path!r} differs from the sources the repair session started from, the inductive gate was not "
        f"PASS at the start, and this candidate passes it ({status}): a code change that the inductive proof "
        "depends on is never accepted or written back by repair; a human must review it",
        file=path,
    )


@dataclass(frozen=True)
class InductiveCodeGuard:
    spec: Spec
    armed: bool
    initial_sources: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def start(cls, spec: Spec, initial: VerificationResult, sources: Mapping[str, str]) -> InductiveCodeGuard:
        return cls(spec, inductive_open(spec, initial), dict(sources))

    def code_paths(self, candidate: Mapping[str, str]) -> list[str]:
        contracts = self.spec.inductive.contracts if self.spec.inductive is not None else None
        return [path for path in changed_paths(dict(self.initial_sources), dict(candidate)) if path != contracts]

    def blocked(self, candidate: Mapping[str, str], vr: VerificationResult) -> PolicyDecision | None:
        if not self.armed or not _inductive_passes(vr):
            return None
        edited = self.code_paths(candidate)
        if not edited:
            return None
        return PolicyDecision(allowed=False, violations=[_violation(path, vr.status.value) for path in edited])


def _blocked(rounds: Iterable[RepairRound]) -> list[int]:
    return [
        r.round
        for r in rounds
        if r.policy is not None and any(v.rule == INDUCTIVE_CODE_EDIT_RULE for v in r.policy.violations)
    ]


def blocked_rounds_note(rounds: Iterable[RepairRound]) -> str:
    numbers = _blocked(rounds)
    if not numbers:
        return ""
    shown = ", ".join(str(number) for number in numbers)
    return (
        f"; round(s) {shown} passed the inductive gate only on C code changed by this repair session while the "
        f"inductive gate was open ({INDUCTIVE_CODE_EDIT_RULE}): not accepted, a human must review the change "
        f"(sources in repair/round-<n>/{BLOCKED_CANDIDATE_DIR})"
    )
