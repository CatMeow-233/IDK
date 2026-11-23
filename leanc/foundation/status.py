from __future__ import annotations

from enum import Enum
from typing import Iterable


class Status(str, Enum):
    COMPILE_ERROR = "COMPILE_ERROR"
    STATIC_ANALYSIS_WARNING = "STATIC_ANALYSIS_WARNING"
    RUNTIME_TEST_FAILED = "RUNTIME_TEST_FAILED"
    COUNTEREXAMPLE_FOUND = "COUNTEREXAMPLE_FOUND"
    VERIFIED_BOUNDED = "VERIFIED_BOUNDED"
    VERIFIED_INDUCTIVE = "VERIFIED_INDUCTIVE"
    PROVED = "PROVED"
    UNKNOWN = "UNKNOWN"
    TIMEOUT = "TIMEOUT"
    TOOL_ERROR = "TOOL_ERROR"
    SPEC_UNAPPROVED = "SPEC_UNAPPROVED"
    REPAIR_EXHAUSTED = "REPAIR_EXHAUSTED"

    def __str__(self) -> str:
        return self.value


class Outcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"

    def __str__(self) -> str:
        return self.value


PRECEDENCE_WORST_FIRST: tuple[Status, ...] = (
    Status.SPEC_UNAPPROVED,
    Status.COMPILE_ERROR,
    Status.COUNTEREXAMPLE_FOUND,
    Status.RUNTIME_TEST_FAILED,
    Status.STATIC_ANALYSIS_WARNING,
    Status.TOOL_ERROR,
    Status.TIMEOUT,
    Status.UNKNOWN,
    Status.VERIFIED_BOUNDED,
    Status.VERIFIED_INDUCTIVE,
    Status.PROVED,
)

SUCCESS_STATUSES = frozenset({Status.VERIFIED_BOUNDED, Status.VERIFIED_INDUCTIVE, Status.PROVED})

DEFINITE_FAILURES = frozenset(
    {
        Status.COMPILE_ERROR,
        Status.COUNTEREXAMPLE_FOUND,
        Status.RUNTIME_TEST_FAILED,
        Status.STATIC_ANALYSIS_WARNING,
    }
)

REPAIRABLE = DEFINITE_FAILURES

SUCCESS_EXIT_CODE = 0
EXIT_CODES: dict[Status, int] = {
    Status.VERIFIED_BOUNDED: SUCCESS_EXIT_CODE,
    Status.VERIFIED_INDUCTIVE: SUCCESS_EXIT_CODE,
    Status.PROVED: SUCCESS_EXIT_CODE,
    Status.COMPILE_ERROR: 10,
    Status.STATIC_ANALYSIS_WARNING: 11,
    Status.RUNTIME_TEST_FAILED: 12,
    Status.COUNTEREXAMPLE_FOUND: 13,
    Status.UNKNOWN: 20,
    Status.TIMEOUT: 21,
    Status.TOOL_ERROR: 30,
    Status.SPEC_UNAPPROVED: 40,
    Status.REPAIR_EXHAUSTED: 50,
}
EXIT_USAGE = 2
EXIT_INTERNAL = 70

_WORKFLOW_STATUSES_RANKED_AFTER_SPEC_UNAPPROVED: tuple[Status, ...] = (Status.REPAIR_EXHAUSTED,)
_RANK_ORDER: tuple[Status, ...] = (
    PRECEDENCE_WORST_FIRST[0],
    *_WORKFLOW_STATUSES_RANKED_AFTER_SPEC_UNAPPROVED,
    *PRECEDENCE_WORST_FIRST[1:],
)

STATUS_OF_FAILED_STAGE: dict[str, Status] = {
    "spec": Status.SPEC_UNAPPROVED,
    "compile": Status.COMPILE_ERROR,
    "static_analysis": Status.STATIC_ANALYSIS_WARNING,
    "integrity": Status.STATIC_ANALYSIS_WARNING,
    "sanitizer": Status.RUNTIME_TEST_FAILED,
    "cbmc": Status.COUNTEREXAMPLE_FOUND,
    "wp": Status.COUNTEREXAMPLE_FOUND,
    "lean": Status.RUNTIME_TEST_FAILED,
    "inductive": Status.UNKNOWN,
    "policy": Status.TOOL_ERROR,
}

_STATUS_OF_UNDECIDED_OUTCOME: dict[Outcome, Status] = {
    Outcome.ERROR: Status.TOOL_ERROR,
    Outcome.TIMEOUT: Status.TIMEOUT,
    Outcome.INCONCLUSIVE: Status.UNKNOWN,
    Outcome.SKIPPED: Status.UNKNOWN,
}


def rank(status: Status) -> int:
    return _RANK_ORDER.index(status)


def worst(statuses: Iterable[Status], default: Status = Status.UNKNOWN) -> Status:
    items = list(statuses)
    if not items:
        return default
    return min(items, key=rank)


def is_success(status: Status) -> bool:
    return status in SUCCESS_STATUSES


def exit_code(status: Status) -> int:
    return EXIT_CODES.get(status, EXIT_INTERNAL)


def outcome_to_status(stage: str, outcome: Outcome) -> Status | None:
    if outcome is Outcome.PASS:
        return None
    if outcome in _STATUS_OF_UNDECIDED_OUTCOME:
        return _STATUS_OF_UNDECIDED_OUTCOME[outcome]
    return STATUS_OF_FAILED_STAGE.get(stage, Status.UNKNOWN)
