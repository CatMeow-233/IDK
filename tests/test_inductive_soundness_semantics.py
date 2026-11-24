from __future__ import annotations

import json
from pathlib import Path

import pytest
from goto_pipeline_support import INDUCTIVE_MODELS, PROPERTY_HOLDS, include_args, run_tool, tool
from inductive_build_support import TALLY_SOURCE
from inductive_gate_support import VERIFY_LABEL
from inductive_soundness_support import (
    BELOW_ASSIGNS,
    below_contract,
    below_gate,
    described,
    failing,
    never_counterexamples,
    only_codes,
)

from leanc.backends.inductive import build as inductive_build
from leanc.foundation.reasons import Reason
from leanc.foundation.status import Outcome, Status
from leanc.toolchain.context import VerifyOptions

pytestmark = pytest.mark.usefixtures("needs_cbmc")

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "inductive"
LIB_FRAME_PROGRAM = FIXTURES / "acceptance" / "mut" / "m2c_lib_frame_late.c"
UNDECLARED_PROGRAM = FIXTURES / "attack" / "undeclared" / "h.c"
MEMSET_LOOP_LINE = 9
FREE_LOOP_LINE = 9
TALLY_LOOP_LINE = 6
STATIC_LOOP_LINE = 8
LOCAL_STATIC_LOOP_LINE = 7
COPY_INDEX_LINE = 16
SCRATCH_OBJECT = "__CPROVER_object_whole(scratch)"
HAVOC_STATEMENT = "__CPROVER_havoc_slice(dst, n);"
MEMSET_ASSIGNS = "memset.assigns."
FREE_FREES = "free.frees."
LOOP_DECREASES = "tally_below.loop_decreases."
HARNESS_BOUND = "assertion 0 <= c && c <= n"
OFFSET_OVERFLOW = "arithmetic overflow on signed + in c + "
TABLE_BOUND = "array 'table' upper bound"
WIDE_INPUT_ASSERTION = "assertion wide != (1ull << 40)"
UNDECLARED_WARNING = "function 'nondet_ulonglong' is not declared"
WIDE_ASSERTION_PROPERTY = "main.assertion.2"
VERBOSE = ("--verbosity", "2")
LEGACY_INSTRUMENTATION = ("--apply-loop-contracts",)
DFCC_INSTRUMENTATION = ("--dfcc", "main", "--apply-loop-contracts")


def _goto_results(cwd: Path, goto: str) -> list[dict]:
    argv = [tool("cbmc"), goto, "--no-library", "--unwind", "4", "--unwinding-assertions", "--json-ui"]
    messages = json.loads(run_tool(argv, cwd).stdout)
    return next(message["result"] for message in messages if "result" in message)


def _not_holding(results: list[dict]) -> list[str]:
    return sorted(result["property"] for result in results if result["status"] != PROPERTY_HOLDS)


@pytest.mark.parametrize(
    "assigns, passes", [(list(BELOW_ASSIGNS), False), ([*BELOW_ASSIGNS, SCRATCH_OBJECT], True)], ids=["outside", "inside"]
)
def test_a_library_memset_is_checked_against_the_loop_assigns_under_dfcc(tmp_path, assigns, passes):
    result = below_gate(tmp_path, below_contract(MEMSET_LOOP_LINE, assigns=assigns), source="lib_memset.c")
    if passes:
        assert result.outcome is Outcome.PASS, result.summary
        return
    assert result.outcome is Outcome.INCONCLUSIVE and result.status is Status.UNKNOWN
    assert only_codes(result) == {Reason.INDUCTIVE_STEP_FAILED}
    assert [failure.location.function for failure in failing(result, MEMSET_ASSIGNS)] == ["memset"]
    assert never_counterexamples(result)


@pytest.mark.parametrize("source, passes", [("lib_free_in_loop.c", False), ("lib_free_after_loop.c", True)])
def test_a_free_inside_a_contracted_loop_fails_its_frees_check(tmp_path, source, passes):
    result = below_gate(tmp_path, below_contract(FREE_LOOP_LINE), source=source)
    if passes:
        assert result.outcome is Outcome.PASS, result.summary
        return
    assert result.outcome is Outcome.INCONCLUSIVE
    assert Reason.INDUCTIVE_STEP_FAILED in only_codes(result)
    assert [failure.location.function for failure in failing(result, FREE_FREES)] == ["free"]
    assert never_counterexamples(result)


def test_loop_contracts_without_dfcc_miss_the_library_write_that_dfcc_reports(tmp_path):
    linked = run_tool([tool("goto-cc"), "-o", "prog.goto", str(INDUCTIVE_MODELS), str(LIB_FRAME_PROGRAM)], tmp_path)
    assert linked.returncode == 0, linked.stderr
    found = {}
    for name, flags in (("legacy", LEGACY_INSTRUMENTATION), ("dfcc", DFCC_INSTRUMENTATION)):
        done = run_tool([tool("goto-instrument"), *flags, "prog.goto", f"{name}.goto"], tmp_path)
        assert done.returncode == 0, done.stdout + done.stderr
        found[name] = _not_holding(_goto_results(tmp_path, f"{name}.goto"))
    assert found["legacy"] == []
    assert [prop for prop in found["dfcc"] if prop.startswith(MEMSET_ASSIGNS)] == found["dfcc"] != []


@pytest.mark.parametrize("decreases", ["i", "n", "n + i"])
def test_a_wrong_decreases_on_a_terminating_loop_is_suspected_non_termination(tmp_path, decreases):
    result = below_gate(tmp_path, below_contract(TALLY_LOOP_LINE, decreases=decreases))
    assert result.outcome is Outcome.INCONCLUSIVE
    assert only_codes(result) == {Reason.SUSPECTED_NON_TERMINATION}
    locations = [(f.location.file, f.location.line) for f in failing(result, LOOP_DECREASES)]
    assert locations == [(TALLY_SOURCE, TALLY_LOOP_LINE)]


def test_the_right_decreases_on_the_same_loop_passes(tmp_path):
    result = below_gate(tmp_path, below_contract(TALLY_LOOP_LINE))
    assert result.outcome is Outcome.PASS, result.summary


@pytest.mark.parametrize(
    "source, line",
    [("static_file.c", STATIC_LOOP_LINE), ("static_local.c", LOCAL_STATIC_LOOP_LINE), ("static_global.c", STATIC_LOOP_LINE)],
)
def test_non_const_statics_start_arbitrary(tmp_path, source, line):
    result = below_gate(tmp_path, below_contract(line), source=source)
    assert result.outcome is Outcome.INCONCLUSIVE
    assert only_codes(result) == {Reason.PROPERTY_UNPROVED}
    assert described(result, HARNESS_BOUND) and described(result, OFFSET_OVERFLOW)
    assert never_counterexamples(result)


def test_a_const_static_keeps_its_initialiser(tmp_path):
    result = below_gate(tmp_path, below_contract(STATIC_LOOP_LINE), source="static_const.c")
    assert result.outcome is Outcome.PASS, result.summary


def _models_without_havoc(tmp_path: Path) -> str:
    text = INDUCTIVE_MODELS.read_text(encoding="utf-8")
    assert text.count(HAVOC_STATEMENT) == 3
    stripped = tmp_path / "assert_only_models.c"
    stripped.write_text(text.replace(HAVOC_STATEMENT, ""), encoding="utf-8")
    return str(stripped)


def test_the_copy_model_havoc_exposes_a_content_dependent_index(tmp_path):
    result = below_gate(tmp_path, below_contract(STATIC_LOOP_LINE), source="copy_index.c")
    assert result.outcome is Outcome.INCONCLUSIVE
    assert [failure.location.line for failure in described(result, TABLE_BOUND)] == [COPY_INDEX_LINE]


def test_without_the_havoc_the_same_index_bug_passes(tmp_path, monkeypatch):
    stripped = _models_without_havoc(tmp_path)
    monkeypatch.setattr(inductive_build, "inductive_models_path", lambda: stripped)
    result = below_gate(tmp_path, below_contract(STATIC_LOOP_LINE), source="copy_index.c")
    assert result.outcome is Outcome.PASS, result.summary


def test_an_undeclared_harness_input_is_inconclusive_before_cbmc(tmp_path):
    result = below_gate(tmp_path, below_contract(TALLY_LOOP_LINE), harness="undeclared_wide.c")
    assert result.outcome is Outcome.INCONCLUSIVE and result.status is Status.UNKNOWN
    assert only_codes(result) == {Reason.UNDECLARED_FUNCTION}
    assert "nondet_ulonglong" in result.summary
    assert VERIFY_LABEL not in result.details["argv"]


def test_the_declared_wide_input_reaches_the_value_the_undeclared_one_hides(tmp_path):
    result = below_gate(tmp_path, below_contract(TALLY_LOOP_LINE), harness="declared_wide.c")
    assert result.outcome is Outcome.INCONCLUSIVE
    assert only_codes(result) == {Reason.PROPERTY_UNPROVED}
    assert [failure.location.function for failure in described(result, WIDE_INPUT_ASSERTION)] == ["main"]


def test_goto_cc_reports_an_undeclared_call_only_when_verbose_and_cbmc_misses_its_bug(tmp_path):
    preprocessed = run_tool([tool("cbmc"), "--preprocess", *include_args(), str(UNDECLARED_PROGRAM)], tmp_path)
    assert preprocessed.returncode == 0, preprocessed.stderr
    (tmp_path / "h.i").write_text(preprocessed.stdout.partition("\n")[2], encoding="utf-8")
    quiet = run_tool([tool("goto-cc"), "-o", "quiet.goto", "h.i"], tmp_path)
    verbose = run_tool([tool("goto-cc"), *VERBOSE, "-o", "verbose.goto", "h.i"], tmp_path)
    assert quiet.returncode == verbose.returncode == 0
    assert UNDECLARED_WARNING not in quiet.stdout + quiet.stderr
    assert UNDECLARED_WARNING in verbose.stdout + verbose.stderr
    statuses = {result["property"]: result["status"] for result in _goto_results(tmp_path, "quiet.goto")}
    assert statuses[WIDE_ASSERTION_PROPERTY] == PROPERTY_HOLDS


def test_a_library_write_outside_assigns_also_fails_in_the_bwrap_sandbox(tmp_path, needs_bwrap):
    options = VerifyOptions(sandbox_preference="auto")
    result = below_gate(tmp_path, below_contract(MEMSET_LOOP_LINE), source="lib_memset.c", options=options)
    assert {invocation.sandbox for invocation in result.invocations} == {"bwrap"}
    assert result.outcome is Outcome.INCONCLUSIVE and only_codes(result) == {Reason.INDUCTIVE_STEP_FAILED}
    assert failing(result, MEMSET_ASSIGNS)
