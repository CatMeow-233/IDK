from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cprover_support import measured_toolchain
from inductive_build_support import TALLY_SOURCE, tally_context
from inductive_gate_support import (
    VACUITY_LABEL,
    VERIFY_LABEL,
    codes,
    load_gate_recording,
    replay_gate,
    results_edit,
    with_class,
    with_description,
)
from inductive_replay_support import mutated

from leanc.backends.cbmc.execution import CbmcRun
from leanc.backends.cbmc.output import interpret_output
from leanc.backends.inductive import CallSite
from leanc.backends.inductive.probes import expected_probes, judge_probes
from leanc.foundation.inductive_scope import HARNESS_NARROWING
from leanc.foundation.models import ToolInvocation
from leanc.foundation.reasons import Reason
from leanc.foundation.runner import ProcResult
from leanc.rules.loop_sites import Origin
from leanc.foundation.status import Outcome, Status
from leanc.specs.model import MANDATORY_CHECKS

HOLDS = "SUCCESS"
VIOLATED = "FAILURE"
EXIT_VIOLATED = 10
EXIT_HOLDS = 0
EXTRA_LOOP = {"name": "tally_clear.0", "sourceLocation": {"file": TALLY_SOURCE, "function": "tally_clear", "line": "33"}}
UNDECLARED_WARNING = "harness/inductive.c:12:1: warning: function 'nondet_ulonglong' is not declared"


@pytest.fixture
def recording() -> list[dict[str, Any]]:
    return load_gate_recording()


def gate(tmp_path, entries):
    ctx = tally_context(tmp_path)
    ctx.tools = measured_toolchain()
    result, _ = replay_gate(ctx, entries)
    return result


def vacuity_edit(entries, transform):
    return mutated(entries, VACUITY_LABEL, results_edit(transform))


def set_status(description, status, count=None):
    def change(results):
        chosen = with_description(results, description)
        assert chosen, description
        for result in chosen[:count]:
            result["status"] = status

    return change


def drop(description):
    def change(results):
        assert with_description(results, description), description
        results[:] = [result for result in results if result["description"] != description]

    return change


PROBES = [
    "leanc_function:tally_max",
    "leanc_loop:src/tally.c:18",
    "leanc_return:tally_clear",
    "leanc_reach:max_returned",
]


@pytest.mark.parametrize("probe", PROBES)
def test_an_unreached_probe_is_a_vacuous_harness(tmp_path, recording, probe):
    result = gate(tmp_path, vacuity_edit(recording, set_status(probe, HOLDS, 1)))
    assert result.outcome is Outcome.INCONCLUSIVE and result.status is Status.UNKNOWN
    assert codes(result) == [Reason.VACUOUS_HARNESS]
    assert result.details["vacuity"]["status"] == "vacuous"
    (failure,) = result.failures
    assert failure.kind == "unproved" and failure.id.startswith("inductive:vacuity:")


@pytest.mark.parametrize("probe", PROBES)
def test_an_unreported_probe_is_a_vacuous_harness(tmp_path, recording, probe):
    result = gate(tmp_path, vacuity_edit(recording, drop(probe)))
    assert result.outcome is Outcome.INCONCLUSIVE and codes(result) == [Reason.VACUOUS_HARNESS]
    assert "not reported" in result.summary


def probe_run(results: list[dict]) -> CbmcRun:
    stdout = json.dumps([{"result": results}])
    proc = ProcResult(["cbmc"], "/", EXIT_VIOLATED, None, False, False, 0.0, stdout, "")
    invocation = ToolInvocation(tool="cbmc", argv=["cbmc"], cwd="/")
    return CbmcRun("inductive vacuity", ["cbmc"], proc, Path("raw.json"), "raw.json", interpret_output(stdout), invocation)


def probe_result(description: str, line: int, status: str) -> dict:
    return {
        "property": f"main.assertion.{line}",
        "description": description,
        "status": status,
        "sourceLocation": {"file": "harness/inductive.c", "line": str(line), "function": "main"},
    }


def call(line: int) -> CallSite:
    return CallSite("tally_below", 0, 0, Origin("harness/inductive.c", line))


REACH_HARNESS = 'int main(void) { LEANC_REACH("end"); return 0; }'
REACHED_END = probe_result("leanc_reach:end", 30, VIOLATED)


def judged_returns(*statuses: tuple[int, str]):
    (expected,) = expected_probes((), (), (call(15), call(20)))
    results = [probe_result("leanc_return:tally_below", line, status) for line, status in statuses]
    return expected, judge_probes(probe_run([*results, REACHED_END]), REACH_HARNESS, "harness/inductive.c", [expected], False)


def test_return_probes_are_expected_once_per_call_site():
    expected, both = judged_returns((15, VIOLATED), (20, VIOLATED))
    assert (expected.label, expected.count, expected.files) == ("leanc_return:tally_below", 2, frozenset({"harness/inductive.c"}))
    assert both.problem is None and both.findings == [] and both.details["status"] == "ok"
    for statuses in (((15, VIOLATED),), ((15, VIOLATED), (20, HOLDS))):
        _, judged = judged_returns(*statuses)
        assert [finding.reason for finding in judged.findings] == [Reason.VACUOUS_HARNESS]


def test_an_undecided_probe_is_an_error(tmp_path, recording):
    result = gate(tmp_path, vacuity_edit(recording, set_status("leanc_function:tally_below", "UNKNOWN")))
    assert result.outcome is Outcome.ERROR and "leanc_function:tally_below" in result.summary


def test_a_vacuity_run_reporting_no_probe_is_vacuity_unchecked(tmp_path, recording):
    def strip(results):
        results[:] = [result for result in results if not result["description"].startswith("leanc_") or "claimed" in result["description"]]

    edited = mutated(vacuity_edit(recording, strip), VACUITY_LABEL, lambda entry: entry.update(exit_code=EXIT_HOLDS))
    result = gate(tmp_path, edited)
    assert result.outcome is Outcome.INCONCLUSIVE and codes(result) == [Reason.VACUITY_UNCHECKED]
    assert result.details["vacuity"]["status"] == "unchecked"


def test_a_probe_label_lean_c_did_not_insert_is_an_error(tmp_path, recording):
    def forge(results):
        extra = json.loads(json.dumps(with_description(results, "leanc_function:tally_max")[0]))
        extra["description"] = "leanc_function:not_claimed"
        results.append(extra)

    assert gate(tmp_path, vacuity_edit(recording, forge)).outcome is Outcome.ERROR


def test_a_probe_reported_outside_its_file_is_an_error(tmp_path, recording):
    def move(results):
        for result in with_description(results, "leanc_function:tally_max"):
            result["sourceLocation"]["file"] = "harness/inductive.c"

    result = gate(tmp_path, vacuity_edit(recording, move))
    assert result.outcome is Outcome.ERROR and "outside src/tally.c" in result.summary


def test_vacuity_ignores_contract_failures_of_its_own_run(tmp_path, recording):
    def fail_step(results):
        for result in with_class(results, "loop_invariant_step"):
            result["status"] = VIOLATED

    assert gate(tmp_path, vacuity_edit(recording, fail_step)).outcome is Outcome.PASS


def failing_step(entries):
    def fail_step(results):
        for result in with_class(results, "loop_invariant_step")[:1]:
            result["status"] = VIOLATED

    edited = mutated(entries, VERIFY_LABEL, results_edit(fail_step))
    return mutated(edited, VERIFY_LABEL, lambda entry: entry.update(exit_code=EXIT_VIOLATED))


def test_vacuity_still_runs_after_unproved_properties_and_both_codes_are_reported(tmp_path, recording):
    edited = vacuity_edit(failing_step(recording), set_status("leanc_return:tally_below", HOLDS))
    result = gate(tmp_path, edited)
    assert result.outcome is Outcome.INCONCLUSIVE
    assert codes(result) == [Reason.INDUCTIVE_STEP_FAILED, Reason.VACUOUS_HARNESS]
    assert len(result.failures) == 2


def test_a_vacuity_timeout_after_unproved_properties_keeps_them(tmp_path, recording):
    edited = mutated(failing_step(recording), VACUITY_LABEL, lambda entry: entry.update(timed_out=True))
    result = gate(tmp_path, edited)
    assert result.outcome is Outcome.TIMEOUT
    assert codes(result) == [Reason.GATE_TIMEOUT, Reason.INDUCTIVE_STEP_FAILED]
    assert [failure.kind for failure in result.failures] == ["unproved"]
    assert "Also found before the gate stopped" in result.summary


def build_edit(label, change):
    def apply(entries):
        return mutated(entries, label, change)

    return apply


def residual(entry):
    messages = json.loads(entry["stdout"])
    next(message["loops"] for message in messages if "loops" in message).append(EXTRA_LOOP)
    entry["stdout"] = json.dumps(messages)


def undeclared(entry):
    entry["stderr"] = UNDECLARED_WARNING + "\n"


@pytest.mark.parametrize(
    "label, change, outcome, expected",
    [
        ("verify loops", residual, Outcome.INCONCLUSIVE, [Reason.LOOP_WITHOUT_CONTRACT]),
        ("verify compile trusted_1_harness", undeclared, Outcome.INCONCLUSIVE, [Reason.UNDECLARED_FUNCTION]),
        ("verify dfcc", lambda entry: entry.update(exit_code=134), Outcome.ERROR, [Reason.TOOL_ERROR]),
    ],
)
def test_a_build_problem_stops_the_gate_with_its_reason_code(tmp_path, recording, label, change, outcome, expected):
    result = gate(tmp_path, build_edit(label, change)(recording))
    assert result.outcome is outcome and codes(result) == expected
    assert VERIFY_LABEL not in result.details["argv"]


def test_contracts_rejected_by_the_build_have_their_code(tmp_path):
    ctx = tally_context(tmp_path, contracts=json.dumps({"loops": [{"file": TALLY_SOURCE, "line": 6}]}))
    ctx.tools = measured_toolchain()
    result, _ = replay_gate(ctx, [])
    assert result.outcome is Outcome.INCONCLUSIVE and codes(result) == [Reason.CONTRACT_REJECTED]
    assert "decreases" in result.summary


def test_the_pass_scope_names_everything_that_was_and_was_not_proved(tmp_path, recording):
    assumptions = gate(tmp_path, recording).details["assumptions"]
    text = "\n".join(assumptions)
    for needed in (
        "tally_below(), tally_max(), tally_clear()",
        "src/tally.c:6 in tally_below()",
        "src/tally.c:18 in tally_max()",
        "src/tally.c:29 in tally_clear()",
        "harness/inductive.c:10: 1 <= n && n <= 1000000",
        "harness/inductive.c:12: a != ",
        "mandatory CBMC check classes only (" + ", ".join(MANDATORY_CHECKS) + ")",
        "at most one call of a claimed function per path",
        "functions that are not claimed have only the bounded CBMC result",
        "overflows a member array into a neighbouring field or the padding of the same object",
        "a copy into a string literal",
        "strlen, strcpy, strncpy, strncat",
        "is not PROVED",
        "static objects start arbitrary",
        "allocations never fail",
        "harness/inductive.c (sha256 ",
        ") as written, with at most one call of a claimed function per path, satisfies the harness assertions",
        "the LEANC_ASSUMEs and __VERIFIER_assumes only, an upper bound of the covered states",
        f"known gap (not detected): {HARNESS_NARROWING}",
    ):
        assert needed in text, needed
    for overclaim in ("assumptions admit", "every state of the harness inputs"):
        assert overclaim not in text, overclaim
    assert "#" not in text.split("harness assumptions", 1)[1].split("\n", 1)[0]


def test_a_failed_gate_makes_no_inductive_claim(tmp_path, recording):
    result = gate(tmp_path, failing_step(recording))
    (statement,) = result.details["assumptions"]
    assert "establishes no inductive claim" in statement and "proved by induction" not in statement


def test_scope_details_record_the_proof_inputs(tmp_path, recording):
    result = gate(tmp_path, recording)
    scope = result.details["scope"]
    assert scope["functions"] == ["tally_below", "tally_max", "tally_clear"]
    assert scope["check_classes"] == list(MANDATORY_CHECKS)
    assert len(scope["harness"]["sha256"]) == 64 and len(scope["contracts"]["sha256"]) == 64
    assert scope["loops"][0]["decreases"] and scope["loops"][0]["assigns"] == ["i", "c"]
    assert scope["property_classes"]["loop_decreases"] == 3
    assert result.details["harness_assumptions"] == scope["harness_assumptions"]
    assert scope["known_gaps"][0] == f"known gap (not detected): {HARNESS_NARROWING}"
