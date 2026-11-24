from __future__ import annotations

import json
import subprocess

import pytest
from inductive_build_support import CBMC_TIMEOUT_S, TALLY, tally_context, tally_spec_data
from inductive_gate_support import (
    CBMC_LABELS,
    TALLY_CBMC_RECORDING,
    codes,
    iset_context,
    record_gate,
)
from inductive_replay_support import load_recording

from leanc.backends.inductive import build_inductive, run_inductive
from leanc.backends.inductive.program import BuildMode
from leanc.backends.inductive.runs import cbmc_argv, inductive_unwind
from leanc.backends.inductive.steps import ToolSteps
from leanc.foundation.reasons import Reason
from leanc.foundation.status import Outcome, Status
from leanc.toolchain.context import VerifyOptions

pytestmark = pytest.mark.usefixtures("needs_cbmc")

BELOW = ("tally_below",)
TALLY_SOURCE = "src/tally.c"
ISET_LOOP = "src/iset.c:11"
NO_LIBRARY = "--no-library"
SLOW_RUN_TIMEOUT_S = 900


def below_contract(line: int, **fields) -> str:
    entry = {"file": TALLY_SOURCE, "line": line, "invariant": ["0 <= i", "i <= n", "0 <= c", "c <= i"], "assigns": ["i", "c"], "decreases": "n - i"}
    entry.update(fields)
    return json.dumps({"loops": [entry]})


def tally_gate(tmp_path, *, harness="branches.c", functions=("tally_below", "tally_max", "tally_clear"), source=None, contracts=None, **verification):
    data = tally_spec_data(functions, **verification)
    project = {"harness": harness, "contracts": contracts}
    if source is not None:
        project["source"] = TALLY / "src" / source
    return run_inductive(tally_context(tmp_path, data=data, **project))


def _shape(entries):
    shape = {}
    for entry in entries:
        if entry["label"] in CBMC_LABELS:
            results = next(message["result"] for message in json.loads(entry["stdout"]) if "result" in message)
            shape[entry["label"]] = (entry["exit_code"], sorted((result["property"], result["status"]) for result in results))
    return shape


def test_tally_passes_and_the_recorded_cbmc_outputs_match_the_installed_tools(tmp_path):
    result, entries = record_gate(tally_context(tmp_path, data=tally_spec_data(timeout_s=SLOW_RUN_TIMEOUT_S)))
    assert result.outcome is Outcome.PASS, result.summary
    assert codes(result) == [] and result.failures == []
    assert _shape(entries) == _shape(load_recording(TALLY_CBMC_RECORDING))


def test_iset_fixed_lower_bound_is_proved_for_the_claimed_functions(tmp_path):
    result = run_inductive(iset_context(tmp_path, "fixed"))
    assert result.outcome is Outcome.PASS, result.summary
    assert [loop["cbmc_loop"] for loop in result.details["scope"]["loops"]] == ["lower_bound.0"]
    assert f"{ISET_LOOP} in lower_bound()" in "\n".join(result.details["assumptions"])


def test_iset_buggy_midpoint_is_inconclusive_with_step_and_termination_codes(tmp_path):
    result = run_inductive(iset_context(tmp_path, "buggy"))
    assert result.outcome is Outcome.INCONCLUSIVE and result.status is Status.UNKNOWN
    found = codes(result)
    assert {Reason.INDUCTIVE_STEP_FAILED, Reason.SUSPECTED_NON_TERMINATION, Reason.PROPERTY_UNPROVED} <= set(found)
    termination = [code for code in result.details["reason_codes"] if code["code"] == Reason.SUSPECTED_NON_TERMINATION.value]
    assert [(code["location"]["file"], code["location"]["line"]) for code in termination] == [("src/iset.c", 11)]
    assert all(failure.kind == "unproved" for failure in result.failures)
    assert any("lo + hi" in failure.description for failure in result.failures)


def test_a_non_terminating_loop_without_decreases_is_rejected(tmp_path):
    contract = json.loads(below_contract(7, invariant=["0 <= lo", "lo <= hi", "hi <= n"], assigns=["lo", "hi"]))
    del contract["loops"][0]["decreases"]
    result = tally_gate(tmp_path, harness="return_only.c", functions=BELOW, source="stall.c", contracts=json.dumps(contract))
    assert result.outcome is Outcome.INCONCLUSIVE and codes(result) == [Reason.CONTRACT_REJECTED]


def test_a_non_terminating_loop_with_a_decreases_clause_is_suspected_non_terminating(tmp_path):
    contract = below_contract(7, invariant=["0 <= lo", "lo <= hi", "hi <= n"], assigns=["lo", "hi"], decreases="hi - lo")
    result = tally_gate(tmp_path, harness="return_only.c", functions=BELOW, source="stall.c", contracts=contract)
    assert result.outcome is Outcome.INCONCLUSIVE and codes(result) == [Reason.SUSPECTED_NON_TERMINATION]


def test_a_claimed_function_that_never_returns_is_a_vacuous_harness(tmp_path):
    result = tally_gate(tmp_path, harness="return_only.c", functions=BELOW, source="assume_end.c", contracts=below_contract(6))
    assert result.outcome is Outcome.INCONCLUSIVE and codes(result) == [Reason.VACUOUS_HARNESS]
    assert result.details["vacuity"]["inserted"]["leanc_return:tally_below"]["reached"] == 0
    assert result.details["vacuity"]["probes"] == {"end": True}


def test_a_claimed_function_with_an_infinite_loop_is_also_a_vacuous_harness(tmp_path):
    contract = below_contract(6, invariant=["0 <= c", "c <= 1"], assigns=["c"], decreases="n")
    result = tally_gate(tmp_path, harness="return_only.c", functions=BELOW, source="spin.c", contracts=contract)
    assert result.outcome is Outcome.INCONCLUSIVE
    assert codes(result) == [Reason.SUSPECTED_NON_TERMINATION, Reason.VACUOUS_HARNESS]


def test_the_return_only_harness_passes_with_the_terminating_source(tmp_path):
    result = tally_gate(tmp_path, harness="return_only.c", functions=BELOW, contracts=below_contract(6))
    assert result.outcome is Outcome.PASS, result.summary


def test_the_gate_passes_in_the_bwrap_sandbox(tmp_path, needs_bwrap):
    options = VerifyOptions(sandbox_preference="auto")
    ctx = tally_context(tmp_path, harness="return_only.c", data=tally_spec_data(BELOW), contracts=below_contract(6), options=options)
    result = run_inductive(ctx)
    assert result.outcome is Outcome.PASS, result.summary
    assert {invocation.sandbox for invocation in result.invocations} == {"bwrap"}
    assert result.details["sandbox"] == "bwrap"


def test_a_multi_line_loop_header_is_evidenced_by_its_keyword_line(tmp_path):
    result = tally_gate(tmp_path, harness="return_only.c", functions=BELOW, source="multiline.c", contracts=below_contract(6))
    assert result.outcome is Outcome.PASS, result.summary
    assert result.details["scope"]["loops"][0]["cbmc_loop"] == "tally_below.0"


def test_a_call_of_a_function_without_body_is_an_unmodelled_callee(tmp_path):
    result = tally_gate(tmp_path, harness="return_only.c", functions=BELOW, source="bodiless.c", contracts=below_contract(8))
    assert result.outcome is Outcome.INCONCLUSIVE and codes(result) == [Reason.UNMODELLED_CALLEE, Reason.VACUOUS_HARNESS]
    assert "external_weight" in result.summary


@pytest.mark.parametrize(
    "harness, functions",
    [
        ("sequential.c", ("tally_below", "tally_max", "tally_clear")),
        ("nested.c", ("tally_below", "tally_max")),
        ("helper_twice.c", BELOW),
        ("loop_calls.c", ("tally_clear",)),
    ],
)
def test_a_second_claimed_call_on_one_path_is_an_error(tmp_path, harness, functions):
    result = tally_gate(tmp_path, harness=harness, functions=functions)
    assert result.outcome is Outcome.ERROR and "structural rules" in result.summary
    assert "leanc_claimed_call:" in result.summary


def test_a_slow_run_times_out_and_never_passes(tmp_path):
    result = tally_gate(tmp_path, timeout_s=1)
    assert result.outcome is Outcome.TIMEOUT and result.status is Status.TIMEOUT
    assert codes(result)[0] is Reason.GATE_TIMEOUT and "timed out" in result.summary


def cbmc_statuses(argv):
    done = subprocess.run(argv, capture_output=True, text=True, timeout=CBMC_TIMEOUT_S, check=False)
    results = next(message["result"] for message in json.loads(done.stdout) if "result" in message)
    return done.returncode, sorted((result["property"], result["status"]) for result in results)


def cbmc_messages(argv):
    done = subprocess.run(argv, capture_output=True, text=True, timeout=CBMC_TIMEOUT_S, check=False)
    return done.returncode, json.loads(done.stdout)


def cbmc_statuses(argv):
    exit_code, messages = cbmc_messages(argv)
    results = next(message["result"] for message in messages if "result" in message)
    return exit_code, sorted((result["property"], result["status"]) for result in results)


def without_no_library(argv):
    assert argv[2] == NO_LIBRARY
    return [*argv[:2], *argv[3:]]


def built_argvs(ctx):
    build = build_inductive(ctx)
    assert build.built, build.problem
    steps = ToolSteps.start(ctx)
    unwind = inductive_unwind(ctx.spec, build.contracts)
    return [cbmc_argv(steps, mode, program.instrumented, unwind) for mode, program in ((BuildMode.VERIFY, build.verify), (BuildMode.VACUITY, build.vacuity))]


@pytest.mark.parametrize("source", ["strlen.c", "bodiless.c"])
def test_no_library_keeps_the_results_of_programs_that_link_their_library_calls(tmp_path, source):
    contracts = below_contract(8 if source == "bodiless.c" else 6)
    ctx = tally_context(tmp_path, harness="return_only.c", data=tally_spec_data(BELOW), source=TALLY / "src" / source, contracts=contracts)
    for argv in built_argvs(ctx):
        assert cbmc_statuses(argv) == cbmc_statuses(without_no_library(argv))


def cbmc_problems(argv):
    _, messages = cbmc_messages(argv)
    return " ".join(str(message.get("messageText")) for message in messages if message.get("messageType") in ("ERROR", "WARNING"))


def test_without_no_library_cbmc_links_library_bodies_after_dfcc(tmp_path):
    for argv in built_argvs(iset_context(tmp_path, "fixed")):
        assert "Linking library function 'realloc' failed" in cbmc_problems(without_no_library(argv))
        assert cbmc_problems(argv) == ""


def test_library_string_functions_fail_closed(tmp_path):
    result = tally_gate(tmp_path, harness="return_only.c", functions=BELOW, source="strlen.c", contracts=below_contract(6))
    assert result.outcome is Outcome.INCONCLUSIVE
    assert {Reason.INDUCTIVE_STEP_FAILED, Reason.UNWIND_INSUFFICIENT} <= set(codes(result))
    assert any(failure.location and failure.location.function == "strlen" for failure in result.failures)
