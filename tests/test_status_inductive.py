from __future__ import annotations

import re

import pytest
from status_support import gate_stages, stage_result

from leanc.cli import EPILOG
from leanc.foundation.inductive_scope import HARNESS_NARROWING
from leanc.foundation.models import StageResult
from leanc.foundation.status import (
    EXIT_CODES,
    PRECEDENCE_WORST_FIRST,
    STATUS_OF_FAILED_STAGE,
    SUCCESS_STATUSES,
    Outcome,
    Status,
    exit_code,
    is_success,
    outcome_to_status,
    rank,
    worst,
)
from leanc.llm.mock import INDUCTIVE_SUMMARY_SCOPE
from leanc.llm.prompts import EXPLAIN_SYSTEM
from leanc.pipeline.controller import (
    FORMAL_GATE_DID_NOT_PASS_REASON,
    INDUCTIVE_LIMITATION,
    NO_FORMAL_GATE_REASONS,
    PROVED_REASON,
    VERIFIED_BOUNDED_REASON,
    VERIFIED_INDUCTIVE_REASON,
    aggregate,
)
from leanc.reports.markdown import STATUS_MEANING
from leanc.specs.model import INDUCTIVE_GATE, MANDATORY_CHECKS

PASS = Outcome.PASS
FAIL = Outcome.FAIL
INCONCLUSIVE = Outcome.INCONCLUSIVE
ERROR = Outcome.ERROR
TIMEOUT = Outcome.TIMEOUT
SKIPPED = Outcome.SKIPPED
ABSENT = None

BOUNDED = Status.VERIFIED_BOUNDED
INDUCTIVE = Status.VERIFIED_INDUCTIVE
PROVED = Status.PROVED
UNKNOWN = Status.UNKNOWN
TOOL_ERROR = Status.TOOL_ERROR
TIMED_OUT = Status.TIMEOUT
COUNTEREXAMPLE = Status.COUNTEREXAMPLE_FOUND

PRE_INDUCTIVE_RANK_ORDER = (
    Status.SPEC_UNAPPROVED,
    Status.REPAIR_EXHAUSTED,
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
PRE_INDUCTIVE_EXIT_CODES = {
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

AGGREGATE_MATRIX = [
    (PASS, PASS, ABSENT, INDUCTIVE),
    (PASS, INCONCLUSIVE, ABSENT, UNKNOWN),
    (PASS, ERROR, ABSENT, TOOL_ERROR),
    (PASS, TIMEOUT, ABSENT, TIMED_OUT),
    (PASS, SKIPPED, ABSENT, UNKNOWN),
    (PASS, ABSENT, ABSENT, BOUNDED),
    (INCONCLUSIVE, PASS, ABSENT, UNKNOWN),
    (INCONCLUSIVE, INCONCLUSIVE, ABSENT, UNKNOWN),
    (INCONCLUSIVE, ERROR, ABSENT, TOOL_ERROR),
    (INCONCLUSIVE, TIMEOUT, ABSENT, TIMED_OUT),
    (INCONCLUSIVE, SKIPPED, ABSENT, UNKNOWN),
    (INCONCLUSIVE, ABSENT, ABSENT, UNKNOWN),
    (FAIL, PASS, ABSENT, COUNTEREXAMPLE),
    (FAIL, INCONCLUSIVE, ABSENT, COUNTEREXAMPLE),
    (FAIL, ERROR, ABSENT, COUNTEREXAMPLE),
    (FAIL, TIMEOUT, ABSENT, COUNTEREXAMPLE),
    (FAIL, SKIPPED, ABSENT, COUNTEREXAMPLE),
    (FAIL, ABSENT, ABSENT, COUNTEREXAMPLE),
    (PASS, PASS, PASS, PROVED),
    (PASS, INCONCLUSIVE, PASS, UNKNOWN),
    (PASS, ERROR, PASS, TOOL_ERROR),
    (PASS, TIMEOUT, PASS, TIMED_OUT),
    (PASS, SKIPPED, PASS, UNKNOWN),
    (PASS, ABSENT, PASS, PROVED),
    (INCONCLUSIVE, PASS, PASS, UNKNOWN),
    (INCONCLUSIVE, INCONCLUSIVE, PASS, UNKNOWN),
    (INCONCLUSIVE, ERROR, PASS, TOOL_ERROR),
    (INCONCLUSIVE, TIMEOUT, PASS, TIMED_OUT),
    (INCONCLUSIVE, SKIPPED, PASS, UNKNOWN),
    (INCONCLUSIVE, ABSENT, PASS, UNKNOWN),
    (FAIL, PASS, PASS, COUNTEREXAMPLE),
    (FAIL, INCONCLUSIVE, PASS, COUNTEREXAMPLE),
    (FAIL, ERROR, PASS, COUNTEREXAMPLE),
    (FAIL, TIMEOUT, PASS, COUNTEREXAMPLE),
    (FAIL, SKIPPED, PASS, COUNTEREXAMPLE),
    (FAIL, ABSENT, PASS, COUNTEREXAMPLE),
]

SCOPE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"claimed function",
        r"contracted loop",
        r"every execution of main\(\) of the protected inductive harness(?: `[^`]+`)? as written",
        r"harness assertions that execute on it",
        r"harness code other than leanc_assume also narrows the covered states",
        r"upper bound of the covered states, not the covered set",
        r"review the whole harness",
        r"mandatory check classes only",
        r"functions that are not claimed",
        r"states the harness as written does not reach",
        r"extra verification\.checks classes",
        r"only the bounded cbmc result",
        r"static objects start(?:ing)? arbitrary",
        r"allocations (?:are )?assumed to succeed",
        r"overflows into a neighbouring field of the same object",
        r"or that writes into a string literal",
        r"strlen",
        r"fail closed",
    )
)
NOT_PROVED = re.compile(r"\b(?:not|never)\b[^.;]*\bPROVED\b")
OVERCLAIMS = tuple(
    re.compile(pattern)
    for pattern in (
        r"admitted by the harness assumptions",
        r"harness assumptions admit",
        r"states outside the harness assumptions",
    )
)
NON_REQUIRED_CBMC_OUTCOMES = (FAIL, INCONCLUSIVE, ERROR, TIMEOUT, SKIPPED)


def _epilog_paragraph(prefix: str) -> str:
    return next(p for p in EPILOG.split("\n\n") if p.startswith(prefix))


SCOPED_TEXTS = {
    "aggregate reason": VERIFIED_INDUCTIVE_REASON,
    "status meaning": STATUS_MEANING[Status.VERIFIED_INDUCTIVE],
    "mock summary": INDUCTIVE_SUMMARY_SCOPE,
    "cli epilog": " ".join(_epilog_paragraph("VERIFIED_INDUCTIVE").split()),
    "explain prompt": EXPLAIN_SYSTEM,
    "limitation": INDUCTIVE_LIMITATION,
}
TEXTS_LISTING_CHECK_CLASSES = ("aggregate reason", "status meaning", "mock summary", "limitation")


@pytest.mark.parametrize("cbmc,inductive,wp,expected", AGGREGATE_MATRIX)
def test_aggregate_matrix(cbmc, inductive, wp, expected):
    status, reasons = aggregate(gate_stages(cbmc, inductive, wp), approved=True)
    assert status is expected
    if inductive not in (PASS, ABSENT):
        assert any(r.startswith(f"[{INDUCTIVE_GATE}] {inductive.value}") for r in reasons), reasons
    if expected is INDUCTIVE:
        assert reasons == [VERIFIED_INDUCTIVE_REASON]
    if expected is BOUNDED:
        assert reasons == [VERIFIED_BOUNDED_REASON]
    if expected is PROVED:
        assert reasons == [PROVED_REASON]


def test_aggregate_matrix_covers_every_combination():
    combos = {(c, i, w) for c, i, w, _ in AGGREGATE_MATRIX}
    assert len(combos) == len(AGGREGATE_MATRIX) == 3 * 6 * 2


def test_optional_inductive_pass_does_not_raise_the_bounded_status():
    stages = gate_stages(PASS, ABSENT, ABSENT) + [stage_result(INDUCTIVE_GATE, PASS, required=False)]
    assert aggregate(stages, approved=True)[0] is BOUNDED


def test_inductive_pass_without_cbmc_has_no_formal_verdict():
    status, reasons = aggregate(gate_stages(ABSENT, PASS, ABSENT), approved=True)
    assert status is UNKNOWN
    assert reasons == NO_FORMAL_GATE_REASONS


def test_inductive_pass_with_skipped_cbmc_is_unknown():
    assert aggregate(gate_stages(SKIPPED, PASS, ABSENT), approved=True)[0] is UNKNOWN


@pytest.mark.parametrize("cbmc", NON_REQUIRED_CBMC_OUTCOMES)
def test_required_inductive_pass_needs_a_passing_cbmc_stage_even_when_cbmc_is_not_required(cbmc):
    stages = gate_stages(ABSENT, PASS, ABSENT) + [stage_result("cbmc", cbmc, required=False)]
    status, reasons = aggregate(stages, approved=True)
    assert status is UNKNOWN and status is not INDUCTIVE
    assert reasons == [FORMAL_GATE_DID_NOT_PASS_REASON]


def test_inductive_fail_without_status_is_never_a_counterexample():
    stages = gate_stages(PASS, ABSENT, ABSENT) + [StageResult(stage=INDUCTIVE_GATE, outcome=FAIL, summary="unproved")]
    assert aggregate(stages, approved=True)[0] is UNKNOWN


def test_unapproved_spec_wins_over_inductive_pass():
    assert aggregate(gate_stages(PASS, PASS, ABSENT), approved=False)[0] is Status.SPEC_UNAPPROVED


def test_failed_inductive_stage_maps_to_unknown():
    assert STATUS_OF_FAILED_STAGE[INDUCTIVE_GATE] is UNKNOWN
    assert outcome_to_status(INDUCTIVE_GATE, FAIL) is UNKNOWN


def test_verified_inductive_is_a_success_with_exit_zero():
    assert is_success(INDUCTIVE)
    assert INDUCTIVE in SUCCESS_STATUSES
    assert exit_code(INDUCTIVE) == EXIT_CODES[INDUCTIVE] == 0


def test_existing_exit_codes_are_unchanged():
    assert {s: c for s, c in EXIT_CODES.items() if s is not INDUCTIVE} == PRE_INDUCTIVE_EXIT_CODES


def test_verified_inductive_ranks_between_bounded_and_proved():
    assert PRECEDENCE_WORST_FIRST.index(INDUCTIVE) == PRECEDENCE_WORST_FIRST.index(BOUNDED) + 1
    assert PRECEDENCE_WORST_FIRST.index(PROVED) == PRECEDENCE_WORST_FIRST.index(INDUCTIVE) + 1
    assert rank(BOUNDED) < rank(INDUCTIVE) < rank(PROVED)
    assert worst([INDUCTIVE, BOUNDED]) is BOUNDED
    assert worst([INDUCTIVE, PROVED]) is INDUCTIVE
    assert worst([INDUCTIVE, UNKNOWN]) is UNKNOWN


def test_no_existing_status_changes_relative_rank():
    assert tuple(sorted((s for s in Status if s is not INDUCTIVE), key=rank)) == PRE_INDUCTIVE_RANK_ORDER
    for status in PRE_INDUCTIVE_RANK_ORDER:
        if status is not PROVED:
            assert rank(status) == PRE_INDUCTIVE_RANK_ORDER.index(status)


@pytest.mark.parametrize("label", list(SCOPED_TEXTS))
def test_inductive_texts_state_scope_gaps_and_never_proved(label):
    text = SCOPED_TEXTS[label]
    lowered = " ".join(text.lower().split())
    for pattern in SCOPE_PATTERNS:
        assert pattern.search(lowered), (label, pattern.pattern)
    for pattern in OVERCLAIMS:
        assert not pattern.search(lowered), (label, pattern.pattern)
    assert " ".join(HARNESS_NARROWING.lower().split()) in lowered, label
    assert NOT_PROVED.search(text), label
    if label in TEXTS_LISTING_CHECK_CLASSES:
        for check in MANDATORY_CHECKS:
            assert check in text, (label, check)
