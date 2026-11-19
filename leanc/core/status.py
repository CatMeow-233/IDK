"""Verification status vocabulary and the rules for combining statuses.

The statuses are deliberately explicit.  In particular:

* ``VERIFIED_BOUNDED`` means *only* "no violation of the checked properties was
  found by CBMC for the approved harness, input constraints and unwind bound,
  every loop was fully unwound (unwinding assertions passed) and the harness was
  shown to be non-vacuous".  It is never promoted to ``PROVED`` automatically.
* ``PROVED`` is reserved for engines that produce unbounded proofs (planned for
  V2: Frama-C/WP, Lean 4).  No V1 backend emits it.
* "No counterexample found" is not success by itself: timeouts, insufficient
  unwinding, vacuous harnesses, skipped gates and tool failures all map to
  non-success statuses.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable


class Status(str, Enum):
    COMPILE_ERROR = "COMPILE_ERROR"
    STATIC_ANALYSIS_WARNING = "STATIC_ANALYSIS_WARNING"
    RUNTIME_TEST_FAILED = "RUNTIME_TEST_FAILED"
    COUNTEREXAMPLE_FOUND = "COUNTEREXAMPLE_FOUND"
    VERIFIED_BOUNDED = "VERIFIED_BOUNDED"
    PROVED = "PROVED"
    UNKNOWN = "UNKNOWN"
    TIMEOUT = "TIMEOUT"
    TOOL_ERROR = "TOOL_ERROR"
    SPEC_UNAPPROVED = "SPEC_UNAPPROVED"
    REPAIR_EXHAUSTED = "REPAIR_EXHAUSTED"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class Outcome(str, Enum):
    """Outcome of a single pipeline stage (gate)."""

    PASS = "PASS"
    FAIL = "FAIL"  # definite evidence of a defect (or a blocking policy violation)
    INCONCLUSIVE = "INCONCLUSIVE"  # ran, but cannot support a verdict (unwind, vacuity, ...)
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"  # the tool itself failed / is missing / produced unparsable output
    SKIPPED = "SKIPPED"  # not run (earlier blocking failure, sandbox unavailable, disabled)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# Worst first.  Definite defects dominate inconclusive results, because a known
# defect is the most useful thing to report (and to feed to the repair agent).
PRECEDENCE: tuple[Status, ...] = (
    Status.SPEC_UNAPPROVED,
    Status.COMPILE_ERROR,
    Status.COUNTEREXAMPLE_FOUND,
    Status.RUNTIME_TEST_FAILED,
    Status.STATIC_ANALYSIS_WARNING,
    Status.TOOL_ERROR,
    Status.TIMEOUT,
    Status.UNKNOWN,
    Status.VERIFIED_BOUNDED,
    Status.PROVED,
)

SUCCESS_STATUSES = frozenset({Status.VERIFIED_BOUNDED, Status.PROVED})

DEFINITE_FAILURES = frozenset(
    {
        Status.COMPILE_ERROR,
        Status.COUNTEREXAMPLE_FOUND,
        Status.RUNTIME_TEST_FAILED,
        Status.STATIC_ANALYSIS_WARNING,
    }
)

#: Statuses the repair agent may act on.  Inconclusive results (UNKNOWN,
#: TIMEOUT, TOOL_ERROR) are *not* repairable by editing code under test in a
#: trustworthy way, and SPEC_UNAPPROVED must be resolved by a human.
REPAIRABLE = DEFINITE_FAILURES

# Process exit codes for the CLI.  0 is reserved for success statuses.
EXIT_CODES: dict[Status, int] = {
    Status.VERIFIED_BOUNDED: 0,
    Status.PROVED: 0,
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


_RANK_ORDER: tuple[Status, ...] = (PRECEDENCE[0], Status.REPAIR_EXHAUSTED) + PRECEDENCE[1:]


def rank(status: Status) -> int:
    """Lower rank = worse.  REPAIR_EXHAUSTED is a workflow-level status ranked
    just after SPEC_UNAPPROVED so it can never be mistaken for success."""
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
    """Map a stage outcome to the global status it contributes.

    Returns ``None`` for PASS (a passing non-formal stage contributes nothing; a
    passing formal stage is handled by the controller).
    """
    if outcome is Outcome.PASS:
        return None
    if outcome is Outcome.ERROR:
        return Status.TOOL_ERROR
    if outcome is Outcome.TIMEOUT:
        return Status.TIMEOUT
    if outcome in (Outcome.INCONCLUSIVE, Outcome.SKIPPED):
        return Status.UNKNOWN
    # FAIL
    return {
        "spec": Status.SPEC_UNAPPROVED,
        "compile": Status.COMPILE_ERROR,
        "static_analysis": Status.STATIC_ANALYSIS_WARNING,
        "sanitizer": Status.RUNTIME_TEST_FAILED,
        "cbmc": Status.COUNTEREXAMPLE_FOUND,
        "policy": Status.TOOL_ERROR,
    }.get(stage, Status.UNKNOWN)
