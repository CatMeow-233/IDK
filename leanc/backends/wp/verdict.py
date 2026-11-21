"""The WP gate's verdict once every goal of the report is accounted for."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from leanc.backends.wp.console import ConsoleSummary
from leanc.backends.wp.goals import ContractApplication, GoalTally
from leanc.foundation.models import FailureReport
from leanc.foundation.status import Outcome

MAX_LISTED = 3
MAX_LISTED_ITEM_CHARS = 300
MAX_REFUTATION_CHARS = 600
MAX_VACUITY_CHARS = 400


class Verdict(NamedTuple):
    outcome: Outcome
    summary: str


def excerpt(items: list[str], limit: int = MAX_LISTED) -> str:
    more = f" (+{len(items) - limit} more)" if len(items) > limit else ""
    return "; ".join(item[:MAX_LISTED_ITEM_CHARS] for item in items[:limit]) + more


@dataclass(frozen=True)
class ProofEvidence:
    target: str
    tally: GoalTally
    console: ConsoleSummary
    failures: list[FailureReport]
    missing_specification: list[str]
    contract: ContractApplication
    require_termination: bool
    provers: list[str]

    @property
    def proved_text(self) -> str:
        return f"{self.console.proved}/{self.console.total} goals proved"

    def descriptions(self, kind: str) -> list[str]:
        return [failure.description for failure in self.failures if failure.kind == kind]


def final_verdict(evidence: ProofEvidence) -> Verdict:
    if evidence.tally.invalid:
        return _refuted(evidence)
    problems = _reasons_not_proved(evidence)
    if problems:
        return Verdict(Outcome.INCONCLUSIVE, f"Frama-C/WP: {evidence.proved_text}; " + "; ".join(problems))
    if evidence.console.proved != evidence.console.total:
        return Verdict(
            Outcome.ERROR,
            f"inconsistent WP output: every goal of the JSON report is valid but the console reports "
            f"{evidence.proved_text}; no verdict",
        )
    return _proved(evidence)


def _refuted(evidence: ProofEvidence) -> Verdict:
    unproved = len(evidence.tally.unproved)
    return Verdict(
        Outcome.FAIL,
        f"Frama-C/WP refuted {len(evidence.tally.invalid)} goal(s): "
        + "; ".join(evidence.descriptions("counterexample"))[:MAX_REFUTATION_CHARS]
        + (f"; plus {unproved} unproved goal(s)" if unproved else ""),
    )


def _unproved_listing(unproved: list[dict]) -> str:
    listed = "; ".join(f"{entry['goal']} ({entry['verdict']})" for entry in unproved[:MAX_LISTED])
    more = f" (+{len(unproved) - MAX_LISTED} more)" if len(unproved) > MAX_LISTED else ""
    return f"{len(unproved)} goal(s) not proved: {listed}{more}"


def _reasons_not_proved(evidence: ProofEvidence) -> list[str]:
    target = evidence.target
    tally = evidence.tally
    problems: list[str] = []
    if not evidence.contract.applied:
        problems.append(
            f"the approved contract was not applied to {target}() (no {evidence.contract.missing_clause} goal for it)"
        )
    if tally.unproved:
        problems.append(_unproved_listing(tally.unproved))
    if tally.smoke_failed:
        listed = "; ".join(evidence.descriptions("vacuity"))
        problems.append(
            f"{len(tally.smoke_failed)} smoke test(s) failed - the contract or code is (partly) vacuous: "
            f"{listed[:MAX_VACUITY_CHARS]}"
        )
    if evidence.missing_specification:
        problems.append(
            "called function(s) with neither code nor ACSL specification ("
            + ", ".join(evidence.missing_specification)
            + "): WP would rely on clauses Frama-C generated, not on proved or approved facts"
        )
    if tally.non_smoke == 0 or not tally.target_goals:
        problems.append(f"WP generated no proof obligation for {target}() (is it defined in the editable sources?)")
    if tally.smoke_total == 0:
        problems.append("WP ran no smoke test, so vacuity cannot be excluded")
    return problems


def _proved(evidence: ProofEvidence) -> Verdict:
    tally = evidence.tally
    termination = (
        "termination proved" if evidence.require_termination else "partial correctness (termination not required)"
    )
    return Verdict(
        Outcome.PASS,
        f"Frama-C/WP: all {evidence.console.total} goals proved ({tally.non_smoke - len(tally.unproved)} proof "
        f"obligations incl. RTE guards, {tally.smoke_total} smoke tests passed) for the approved contract of "
        f"{evidence.target}(); {termination}; provers {', '.join(evidence.provers)}",
    )
