from __future__ import annotations

import json
from typing import Any

import pytest
from cprover_support import measured_toolchain
from inductive_build_support import TALLY_SOURCE, tally_context, tally_spec_data
from inductive_gate_support import (
    VACUITY_LABEL,
    VERIFY_LABEL,
    codes,
    load_gate_recording,
    messages_edit,
    replay_gate,
    results_edit,
    supplied_codes,
    with_class,
    with_description,
)
from inductive_replay_support import mutated

from leanc.backends.cbmc.argv import CHECK_FLAGS
from leanc.backends.inductive.evidence import LOOP_PROPERTY_CLASSES
from leanc.foundation.reasons import Reason
from leanc.foundation.status import Outcome, Status
from leanc.specs.model import MANDATORY_CHECKS

NO_LIBRARY = "--no-library"
VIOLATED = "FAILURE"
EXIT_VIOLATED = 10
EXTRA_CHECKS = ("unsigned-overflow", "memory-leak")
CLEAR_LOOP = "tally_clear.0"
BELOW_LOOP = "tally_below.0"


@pytest.fixture
def recording() -> list[dict[str, Any]]:
    return load_gate_recording()


def gate(tmp_path, entries, data=None):
    ctx = tally_context(tmp_path, data=data)
    ctx.tools = measured_toolchain()
    result, _ = replay_gate(ctx, entries)
    return result


def fail(selector):
    def change(results):
        chosen = selector(results)
        assert chosen, "the mutation selects no recorded property"
        for result in chosen:
            result["status"] = VIOLATED

    return change


def verify_edit(entries, transform, exit_code=None):
    edited = mutated(entries, VERIFY_LABEL, results_edit(transform))
    if exit_code is not None:
        edited = mutated(edited, VERIFY_LABEL, lambda entry: entry.update(exit_code=exit_code))
    return edited


def loop_class(results, cls, loop=BELOW_LOOP):
    return [result for result in with_class(results, cls) if result["description"].endswith(f"loop {loop}")]


def test_recorded_gate_passes_with_no_reason_code(tmp_path, recording):
    result = gate(tmp_path, recording)
    assert result.outcome is Outcome.PASS and result.status is None, result.summary
    assert codes(result) == [] and result.details["reason_codes"] == []
    assert result.failures == []
    assert "compile_flags" not in result.details
    assert result.details["vacuity"]["status"] == "ok"
    assert [loop["cbmc_loop"] for loop in result.details["scope"]["loops"]] == [BELOW_LOOP, "tally_max.0", CLEAR_LOOP]
    assert result.details["cbmc_version"] == "5.95.1 (cbmc-5.95.1)"
    for needed in ("tally_below(), tally_max(), tally_clear()", "src/tally.c:6, src/tally.c:18, src/tally.c:29",
                   "2 harness assumption(s)", ", ".join(MANDATORY_CHECKS) + " only", "member array",
                   "string literal", "library string functions", "not PROVED", "every execution of main() of",
                   "as written", "narrowed further by its other code", "review the whole harness"):
        assert needed in result.summary, needed


def test_cbmc_runs_check_the_mandatory_classes_only_with_no_library(tmp_path, recording):
    data = tally_spec_data(checks=list(EXTRA_CHECKS))
    result = gate(tmp_path, recording, data)
    verify, vacuity = result.details["argv"][VERIFY_LABEL], result.details["argv"][VACUITY_LABEL]
    mandatory = [CHECK_FLAGS[check] for check in MANDATORY_CHECKS]
    assert [arg for arg in verify if arg in CHECK_FLAGS.values()] == mandatory
    assert not set(vacuity) & set(CHECK_FLAGS.values())
    for argv in (verify, vacuity):
        assert argv[2] == NO_LIBRARY
        assert argv[argv.index("--unwind") + 1] == "4" and "--unwinding-assertions" in argv and argv[-1] == "--json-ui"
        assert argv[1].endswith("dfcc.goto")
    assert verify[1] != vacuity[1]


@pytest.mark.parametrize("unwind, expected", [(1, "4"), (9, "9")])
def test_unwind_is_the_spec_bound_or_the_widest_assigns_plus_two(tmp_path, recording, unwind, expected):
    result = gate(tmp_path, recording, tally_spec_data(unwind=unwind))
    argv = result.details["argv"][VERIFY_LABEL]
    assert argv[argv.index("--unwind") + 1] == expected and result.details["unwind"] == int(expected)


def test_object_bits_reach_cbmc(tmp_path, recording):
    result = gate(tmp_path, recording, tally_spec_data(object_bits=16))
    for label in (VERIFY_LABEL, VACUITY_LABEL):
        argv = result.details["argv"][label]
        assert argv[argv.index("--object-bits") + 1] == "16"


@pytest.mark.parametrize("label", [VERIFY_LABEL, VACUITY_LABEL])
def test_a_timed_out_run_is_a_timeout_never_a_pass(tmp_path, recording, label):
    result = gate(tmp_path, mutated(recording, label, lambda entry: entry.update(timed_out=True)))
    assert result.outcome is Outcome.TIMEOUT and result.status is Status.TIMEOUT
    assert codes(result)[0] is Reason.GATE_TIMEOUT and "timed out" in result.summary
    assert result.details["assumptions"] == [
        "the inductive gate ended TIMEOUT: it establishes no inductive claim, and VERIFIED_INDUCTIVE needs a "
        "passing required inductive gate"
    ]


@pytest.mark.parametrize("label", [VERIFY_LABEL, VACUITY_LABEL])
def test_a_killed_run_is_an_error(tmp_path, recording, label):
    result = gate(tmp_path, mutated(recording, label, lambda entry: entry.update(signal=9, exit_code=-9)))
    assert result.outcome is Outcome.ERROR and codes(result) == [Reason.TOOL_ERROR]


@pytest.mark.parametrize("label", [VERIFY_LABEL, VACUITY_LABEL])
@pytest.mark.parametrize("text, shown", [("something broke", "something broke"), ("", "without text"), ("  ", "without text")])
def test_an_error_message_is_an_error(tmp_path, recording, label, text, shown):
    error = {"messageType": "ERROR", "messageText": text}
    result = gate(tmp_path, mutated(recording, label, messages_edit(lambda messages: messages.append(error))))
    assert result.outcome is Outcome.ERROR and shown in result.summary


@pytest.mark.parametrize("label", [VERIFY_LABEL, VACUITY_LABEL])
def test_truncated_or_unparsable_output_is_an_error(tmp_path, recording, label):
    result = gate(tmp_path, mutated(recording, label, lambda entry: entry.update(stdout=entry["stdout"][:500])))
    assert result.outcome is Outcome.ERROR


def test_an_exit_code_that_contradicts_the_results_is_an_error(tmp_path, recording):
    result = gate(tmp_path, mutated(recording, VERIFY_LABEL, lambda entry: entry.update(exit_code=6)))
    assert result.outcome is Outcome.ERROR and "exit" in result.summary
    failed = verify_edit(recording, fail(lambda results: loop_class(results, "loop_invariant_step")))
    assert gate(tmp_path / "two", failed).outcome is Outcome.ERROR


def test_an_undecided_property_is_an_error(tmp_path, recording):
    def undecided(results):
        results[-1]["status"] = "UNKNOWN"

    result = gate(tmp_path, verify_edit(recording, undecided))
    assert result.outcome is Outcome.ERROR and "could not decide" in result.summary


def test_zero_checked_properties_is_inconclusive(tmp_path, recording):
    result = gate(tmp_path, verify_edit(recording, lambda results: results.clear()))
    assert result.outcome is Outcome.INCONCLUSIVE and codes(result) == [Reason.GATE_INCONCLUSIVE]


def test_a_neutralised_harness_assertion_is_an_error(tmp_path, recording):
    def drop(results):
        results[:] = [result for result in results if result["description"] != "assertion n >= 1"]

    result = gate(tmp_path, verify_edit(recording, drop))
    assert result.outcome is Outcome.ERROR and "harness assertions missing" in result.summary
    assert result.details["harness_assertions"] == {"in_source": 3, "cbmc_properties": 2}


@pytest.mark.parametrize("description", ["leanc_claimed_call:tally_max", "leanc_assume_after_claimed_call"])
def test_a_broken_harness_rule_on_some_path_is_an_error(tmp_path, recording, description):
    result = gate(tmp_path, verify_edit(recording, fail(lambda r: with_description(r, description)), EXIT_VIOLATED))
    assert result.outcome is Outcome.ERROR and "structural rules" in result.summary
    assert result.failures == []


@pytest.mark.parametrize("missing", LOOP_PROPERTY_CLASSES)
def test_a_missing_loop_class_is_an_error(tmp_path, recording, missing):
    def drop(results):
        results[:] = [result for result in results if result not in loop_class(results, missing, CLEAR_LOOP)]

    result = gate(tmp_path, verify_edit(recording, drop))
    assert result.outcome is Outcome.ERROR and missing in result.summary


def test_a_contract_without_evidence_is_rejected(tmp_path, recording):
    def drop(results):
        results[:] = [result for result in results if not result["description"].endswith(f"loop {CLEAR_LOOP}")]

    result = gate(tmp_path, verify_edit(recording, drop))
    assert result.outcome is Outcome.INCONCLUSIVE and Reason.CONTRACT_REJECTED in codes(result)
    assert "src/tally.c:29" in result.summary


def test_an_evidenced_loop_without_contract_is_an_error(tmp_path, recording):
    def add(results):
        extra = json.loads(json.dumps(loop_class(results, "loop_assigns", CLEAR_LOOP)[0]))
        extra["description"] = extra["description"].replace(CLEAR_LOOP, "tally_clear.1")
        extra["sourceLocation"]["line"] = "40"
        results.append(extra)

    result = gate(tmp_path, verify_edit(recording, add))
    assert result.outcome is Outcome.ERROR and "tally_clear.1" in result.summary


@pytest.mark.parametrize(
    "selector, reason",
    [
        (lambda r: loop_class(r, "loop_invariant_step"), Reason.INDUCTIVE_STEP_FAILED),
        (lambda r: loop_class(r, "loop_invariant_base")[:1], Reason.INDUCTIVE_STEP_FAILED),
        (lambda r: loop_class(r, "loop_assigns")[:1], Reason.INDUCTIVE_STEP_FAILED),
        (lambda r: loop_class(r, "loop_step_unwinding"), Reason.INDUCTIVE_STEP_FAILED),
        (lambda r: with_class(r, "assigns")[:1], Reason.INDUCTIVE_STEP_FAILED),
        (lambda r: loop_class(r, "loop_decreases"), Reason.SUSPECTED_NON_TERMINATION),
        (lambda r: with_class(r, "overflow")[:1], Reason.PROPERTY_UNPROVED),
        (lambda r: with_description(r, "assertion 0 <= c && c <= n"), Reason.PROPERTY_UNPROVED),
    ],
)
def test_a_failed_property_is_inconclusive_with_its_reason_code(tmp_path, recording, selector, reason):
    result = gate(tmp_path, verify_edit(recording, fail(selector), EXIT_VIOLATED))
    assert result.outcome is Outcome.INCONCLUSIVE and result.status is Status.UNKNOWN
    assert codes(result) == [reason] == supplied_codes(result)
    (failure,) = result.failures
    assert failure.kind == "unproved" and failure.id.startswith("inductive:") and failure.inputs == []
    assert reason.value in result.summary


def test_suspected_non_termination_carries_the_loop_location(tmp_path, recording):
    result = gate(tmp_path, verify_edit(recording, fail(lambda r: loop_class(r, "loop_decreases")), EXIT_VIOLATED))
    (code,) = result.details["reason_codes"]
    assert code["location"]["file"] == TALLY_SOURCE and code["location"]["line"] == 6
    assert BELOW_LOOP in code["detail"]


def unwinding(results):
    results.append(
        {
            "property": "main.unwind.0",
            "description": "unwinding assertion loop 0",
            "status": VIOLATED,
            "sourceLocation": {"file": "harness/inductive.c", "line": "9", "function": "main"},
        }
    )


def undefined_callee(results):
    results.append(
        {
            "property": "ext.assertion.1",
            "description": "undefined function should be unreachable",
            "status": VIOLATED,
            "sourceLocation": {"file": TALLY_SOURCE, "line": "3", "function": "ext"},
        }
    )


@pytest.mark.parametrize("transform, reason", [(unwinding, Reason.UNWIND_INSUFFICIENT), (undefined_callee, Reason.UNMODELLED_CALLEE)])
def test_unwinding_and_bodiless_failures_have_their_codes(tmp_path, recording, transform, reason):
    result = gate(tmp_path, verify_edit(recording, transform, EXIT_VIOLATED))
    assert result.outcome is Outcome.INCONCLUSIVE and codes(result) == [reason]


def test_an_unmodelled_bodiless_warning_alone_is_inconclusive(tmp_path, recording):
    warning = {"messageType": "WARNING", "messageText": "no body for function external_weight"}
    result = gate(tmp_path, mutated(recording, VERIFY_LABEL, messages_edit(lambda messages: messages.append(warning))))
    assert result.outcome is Outcome.INCONCLUSIVE and codes(result) == [Reason.UNMODELLED_CALLEE]
    assert result.details["unmodelled_bodiless"] == ["external_weight"]


def test_a_bodiless_function_reported_after_dfcc_is_an_unmodelled_callee(tmp_path, recording):
    warning = "file src/tally.c line 3: no body for function 'mystery'"
    result = gate(tmp_path, mutated(recording, "verify dfcc", lambda entry: entry.update(stderr=warning)))
    assert result.outcome is Outcome.INCONCLUSIVE and codes(result) == [Reason.UNMODELLED_CALLEE]
    assert result.details["unmodelled_bodiless"] == ["mystery"]
