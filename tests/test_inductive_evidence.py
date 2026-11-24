from __future__ import annotations

import json

import pytest
from inductive_build_support import tally_spec_data

from leanc.backends.cbmc.properties import tally_properties
from leanc.backends.inductive.checks import (
    harness_assertion_counts,
    harness_rule_problem,
    missing_assertions_problem,
    nothing_checked_problem,
)
from leanc.backends.inductive.evidence import LOOP_PROPERTY_CLASSES, loop_evidence
from leanc.backends.inductive.findings import failure_reason, property_findings
from leanc.foundation.reasons import Reason
from leanc.foundation.status import Outcome
from leanc.rules.loop_contracts import parse_loop_contracts
from leanc.rules.loop_sites import LoopSite, MatchedLoop
from leanc.specs.model import Spec

SOURCE = "src/tally.c"
HARNESS = "harness/inductive.c"
DESCRIPTIONS = {
    "loop_invariant_base": "Check invariant before entry for loop {loop}",
    "loop_invariant_step": "Check invariant after step for loop {loop}",
    "loop_decreases": "Check variant decreases after step for loop {loop}",
    "loop_assigns": "Check assigns clause inclusion for loop {loop}",
    "loop_step_unwinding": "Check step was unwound for loop {loop}",
}
HARNESS_SOURCE = "int main(void)\n{\n    int x = 1;\n    assert(x == 1);\n    __CPROVER_assert(x > 0, \"positive\");\n    return 0;\n}\n"


def contract_json(*lines: int) -> str:
    return json.dumps(
        {"loops": [{"file": SOURCE, "line": line, "invariant": ["0 <= i"], "assigns": ["i"], "decreases": "n - i"} for line in lines]}
    )


def matched(*sites: tuple[int, int]) -> list[MatchedLoop]:
    spec = Spec.model_validate(tally_spec_data())
    contracts = parse_loop_contracts(spec, contract_json(*(line for line, _ in sites)))
    assert contracts.accepted, contracts.problems
    return [
        MatchedLoop(contract, LoopSite(SOURCE, line, "for", end, 0, 0))
        for contract, (line, end) in zip(contracts.loops, sites)
    ]


def loop_result(cls: str, loop: str, line: int, *, file: str = SOURCE, status: str = "SUCCESS", number: int = 1) -> dict:
    function = loop.split(".")[0]
    return {
        "property": f"{function}.{cls}.{number}",
        "description": DESCRIPTIONS[cls].format(loop=loop),
        "status": status,
        "sourceLocation": {"file": file, "line": str(line), "function": function},
    }


def full_loop(loop: str, line: int, **fields) -> list[dict]:
    return [loop_result(cls, loop, line, **fields) for cls in LOOP_PROPERTY_CLASSES]


def test_every_contract_with_one_loop_carrying_all_five_classes_is_evidenced():
    contracts = matched((6, 6), (18, 18))
    evidence = loop_evidence(full_loop("f.0", 6) + full_loop("g.0", 18), contracts, HARNESS)
    assert evidence.problem is None
    assert evidence.loop_of(f"{SOURCE}:6").name == "f.0" and evidence.loop_of(f"{SOURCE}:18").name == "g.0"


def test_multi_line_header_maps_by_the_keyword_to_header_end_interval():
    evidence = loop_evidence(full_loop("f.0", 8), matched((6, 8)), HARNESS)
    assert evidence.problem is None and evidence.loop_of(f"{SOURCE}:6").name == "f.0"


def test_loop_reported_below_the_header_is_not_the_contracted_loop():
    evidence = loop_evidence(full_loop("f.0", 9), matched((6, 8)), HARNESS)
    assert evidence.problem.outcome is Outcome.ERROR and "matches 0 contract(s)" in evidence.problem.describe()


@pytest.mark.parametrize("missing", LOOP_PROPERTY_CLASSES)
def test_a_missing_loop_class_is_an_error(missing):
    results = [result for result in full_loop("f.0", 6) if f".{missing}." not in result["property"]]
    evidence = loop_evidence(results, matched((6, 6)), HARNESS)
    assert evidence.problem.outcome is Outcome.ERROR and missing in evidence.problem.describe()


def test_contract_without_reported_loop_is_rejected_as_not_reached():
    evidence = loop_evidence(full_loop("f.0", 6), matched((6, 6), (18, 18)), HARNESS)
    assert evidence.problem.outcome is Outcome.INCONCLUSIVE and evidence.problem.reason is Reason.CONTRACT_REJECTED
    assert f"{SOURCE}:18" in evidence.problem.describe() and "remove its contract" in evidence.problem.describe()


def test_loop_with_contract_properties_but_no_contract_is_an_error():
    evidence = loop_evidence(full_loop("f.0", 6) + full_loop("h.0", 40), matched((6, 6)), HARNESS)
    assert evidence.problem.outcome is Outcome.ERROR and "h.0" in evidence.problem.describe()


def test_a_loop_inside_two_contract_intervals_is_an_error():
    evidence = loop_evidence(full_loop("f.0", 7), matched((6, 8), (7, 7)), HARNESS)
    assert evidence.problem.outcome is Outcome.ERROR and "matches 2 contract(s)" in evidence.problem.describe()


def test_two_loops_on_one_contract_are_an_error():
    evidence = loop_evidence(full_loop("f.0", 6) + full_loop("f.1", 6), matched((6, 6)), HARNESS)
    assert evidence.problem.outcome is Outcome.ERROR and "2 CBMC loops" in evidence.problem.describe()


def test_a_loop_reported_in_two_files_is_an_error():
    results = full_loop("f.0", 6)
    results[-1]["sourceLocation"]["file"] = "src/other.c"
    problem = loop_evidence(results, matched((6, 6)), HARNESS).problem
    assert problem.outcome is Outcome.ERROR and "two files" in problem.describe()


def test_loop_properties_in_the_harness_and_library_are_not_matched():
    results = full_loop("f.0", 6) + full_loop("main.0", 12, file=HARNESS) + full_loop("strlen.0", 3, file="<builtin-library-strlen>")
    assert loop_evidence(results, matched((6, 6)), HARNESS).problem is None


def test_loop_property_without_a_loop_name_is_an_error():
    results = full_loop("f.0", 6)
    results[0]["description"] = "Check invariant before entry"
    assert loop_evidence(results, matched((6, 6)), HARNESS).problem.outcome is Outcome.ERROR


def plain(property_id: str, description: str, *, function: str = "f", status: str = "FAILURE", trace=None) -> dict:
    result = {
        "property": property_id,
        "description": description,
        "status": status,
        "sourceLocation": {"file": SOURCE, "line": "7", "function": function},
    }
    if trace is not None:
        result["trace"] = trace
    return result


@pytest.mark.parametrize(
    "result, reason",
    [
        (loop_result("loop_invariant_base", "f.0", 6), Reason.INDUCTIVE_STEP_FAILED),
        (loop_result("loop_invariant_step", "f.0", 6), Reason.INDUCTIVE_STEP_FAILED),
        (loop_result("loop_assigns", "f.0", 6), Reason.INDUCTIVE_STEP_FAILED),
        (loop_result("loop_step_unwinding", "f.0", 6), Reason.INDUCTIVE_STEP_FAILED),
        (plain("f.assigns.3", "Check that x is assignable"), Reason.INDUCTIVE_STEP_FAILED),
        (plain("f.frees.1", "Check that p is freeable"), Reason.INDUCTIVE_STEP_FAILED),
        (loop_result("loop_decreases", "f.0", 6), Reason.SUSPECTED_NON_TERMINATION),
        (plain("main.unwind.0", "unwinding assertion loop 0"), Reason.UNWIND_INSUFFICIENT),
        (
            plain("__CPROVER_contracts_write_set_check_assignment.unwind.1", "unwinding assertion loop 0"),
            Reason.UNWIND_INSUFFICIENT,
        ),
        (plain("ext.assertion.1", "undefined function should be unreachable", function="ext"), Reason.UNMODELLED_CALLEE),
        (plain("ext.overflow.1", "arithmetic overflow on signed + in a + b", function="ext"), Reason.UNMODELLED_CALLEE),
        (
            plain("f.overflow.2", "arithmetic overflow", trace=[{"stepType": "assignment", "lhs": "return_value_ext"}]),
            Reason.UNMODELLED_CALLEE,
        ),
        (plain("f.overflow.1", "arithmetic overflow on signed + in lo + hi"), Reason.PROPERTY_UNPROVED),
        (plain("main.assertion.4", "assertion c <= n", function="main"), Reason.PROPERTY_UNPROVED),
    ],
)
def test_failed_properties_are_classified(result, reason):
    assert failure_reason(result, frozenset({"ext"})) is reason


def test_failures_are_unproved_and_never_counterexamples():
    result = plain("f.overflow.1", "arithmetic overflow on signed + in lo + hi", trace=[{"stepType": "assignment", "lhs": "x"}])
    (finding,) = property_findings([result], frozenset(), "raw/inductive-verify.json")
    failure = finding.failure
    assert failure.kind == "unproved" and failure.id == "inductive:f.overflow.1"
    assert failure.inputs == [] and failure.call_stack == [] and failure.trace == []
    assert "not a counterexample" in failure.explanation
    code = finding.reason_code()
    assert code["code"] == Reason.PROPERTY_UNPROVED.value and code["location"]["line"] == 7


def harness_check(description: str, status: str) -> dict:
    return {
        "property": "main.assertion.9",
        "description": description,
        "status": status,
        "sourceLocation": {"file": HARNESS, "line": "15", "function": "main"},
    }


@pytest.mark.parametrize("description", ["leanc_claimed_call:tally_below", "leanc_assume_after_claimed_call"])
def test_a_failing_harness_rule_check_is_an_error(description):
    assert harness_rule_problem([harness_check(description, "SUCCESS")]) is None
    problem = harness_rule_problem([harness_check(description, "FAILURE")])
    assert problem.outcome is Outcome.ERROR and description in problem.describe()


def test_inserted_checks_do_not_count_as_harness_assertions():
    own = [harness_check("assertion x == 1", "SUCCESS")]
    inserted = [harness_check("leanc_claimed_call:tally_below", "SUCCESS"), harness_check("leanc_assume_after_claimed_call", "SUCCESS")]
    in_source, listed = harness_assertion_counts(own + inserted, HARNESS_SOURCE, HARNESS)
    assert (in_source, listed) == (2, 1)
    assert missing_assertions_problem(in_source, listed, HARNESS).outcome is Outcome.ERROR
    in_source, listed = harness_assertion_counts(own * 2 + inserted, HARNESS_SOURCE, HARNESS)
    assert missing_assertions_problem(in_source, listed, HARNESS) is None


def test_zero_checked_properties_is_inconclusive():
    problem = nothing_checked_problem(tally_properties([plain("main.unwind.0", "unwinding assertion loop 0", status="SUCCESS")]))
    assert problem.outcome is Outcome.INCONCLUSIVE and problem.reason is Reason.GATE_INCONCLUSIVE
