from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from leanc.foundation.models import Location
from leanc.foundation.reasons import Reason
from leanc.foundation.status import Outcome

STAGE = "inductive"
MAX_LISTED_DETAILS = 5
DETAIL_SEPARATOR = "; "


@dataclass(frozen=True)
class BuildProblem:
    outcome: Outcome
    reason: Reason
    summary: str
    details: tuple[str, ...] = ()
    location: Location | None = None

    def describe(self) -> str:
        if not self.details:
            return self.summary
        shown = DETAIL_SEPARATOR.join(self.details[:MAX_LISTED_DETAILS])
        more = len(self.details) - MAX_LISTED_DETAILS
        return f"{self.summary}: {shown}" + (f" (+{more} more)" if more > 0 else "")


def tool_error(summary: str, details: Iterable[str] = (), location: Location | None = None) -> BuildProblem:
    return BuildProblem(Outcome.ERROR, Reason.TOOL_ERROR, summary, tuple(details), location)


def timed_out(summary: str) -> BuildProblem:
    return BuildProblem(Outcome.TIMEOUT, Reason.GATE_TIMEOUT, summary)


def inconclusive(reason: Reason, summary: str, details: Iterable[str] = (), location: Location | None = None) -> BuildProblem:
    return BuildProblem(Outcome.INCONCLUSIVE, reason, summary, tuple(details), location)


def contract_rejected(summary: str, details: Iterable[str] = ()) -> BuildProblem:
    return inconclusive(Reason.CONTRACT_REJECTED, summary, details)
