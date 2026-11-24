from __future__ import annotations

import pytest
from inductive_build_support import TALLY_SOURCE
from inductive_gate_support import VACUITY_LABEL, VERIFY_LABEL
from inductive_soundness_support import below_contract, below_gate, failing, never_counterexamples, only_codes, ring_gate

from leanc.backends.inductive.harness_narrowing import EARLY_EXIT_RULE, PRE_CALL_WRITE_RULE
from leanc.backends.inductive.runs import NO_SELF_LOOPS_FLAG
from leanc.backends.inductive.scope import HARNESS_GAP
from leanc.foundation.reasons import Reason
from leanc.foundation.status import Outcome, Status

pytestmark = pytest.mark.usefixtures("needs_cbmc")

TALLY_LOOP_LINE = 6
HOOKED_LOOP_LINE = 8
STACK_HARNESS = "stack_only.c"
TERMINATION_RULE = "ends the path or jumps out of it without a check"
HARNESS_RULES = "breaks the harness rules"
TRUSTED_SYMBOL_RULES = "breaks the trusted-symbol rules"
DFCC_FREE_CRASH = "no body for function 'free'"
STRLEN_ASSIGNS = "strlen.assigns."
LIBRARY_UNWIND = "__CPROVER_contracts_write_set_check_assignment.unwind."


@pytest.mark.parametrize("harness, call", [("exit_after.c", "exit"), ("abort_after.c", "abort")])
def test_a_harness_that_ends_the_path_after_the_claimed_call_is_an_error(tmp_path, harness, call):
    result = ring_gate(tmp_path, harness)
    assert result.outcome is Outcome.ERROR and result.status is Status.TOOL_ERROR
    assert only_codes(result) == {Reason.TOOL_ERROR}
    assert HARNESS_RULES in result.summary and f"harness/inductive.c:15: {call} {TERMINATION_RULE}" in result.summary


@pytest.mark.parametrize(
    "harness, problem",
    [
        ("early_return.c", f"harness/inductive.c:16: return before the end of main() {EARLY_EXIT_RULE}"),
        ("goto_after.c", f"harness/inductive.c:15: goto in main() {EARLY_EXIT_RULE}"),
        ("precall_clamp.c", "harness/inductive.c:12: = writes in main() after its last LEANC_ASSUME"),
        ("fp_exit.c", f"harness/inductive.c:7: exit {TERMINATION_RULE}"),
    ],
    ids=["early_return", "goto", "pre_call_write", "exit_through_a_function_pointer"],
)
def test_harness_narrowing_that_hides_the_ring_bug_is_an_error(tmp_path, harness, problem):
    result = ring_gate(tmp_path, harness)
    assert result.outcome is Outcome.ERROR and result.status is Status.TOOL_ERROR
    assert only_codes(result) == {Reason.TOOL_ERROR}
    assert HARNESS_RULES in result.summary and problem in result.summary, result.summary


def test_the_pre_call_write_rule_names_the_claimed_call(tmp_path):
    result = ring_gate(tmp_path, "precall_clamp.c")
    assert PRE_CALL_WRITE_RULE in result.summary and "claimed function ring_add at harness/inductive.c:13" in result.summary


def test_a_spin_loop_after_the_claimed_call_is_an_unwinding_failure_not_a_pruned_path(tmp_path):
    result = ring_gate(tmp_path, "spin_after.c")
    assert result.outcome is Outcome.INCONCLUSIVE and result.status is Status.UNKNOWN, result.summary
    assert Reason.UNWIND_INSUFFICIENT in only_codes(result)
    assert failing(result, "main.unwind.") and never_counterexamples(result)
    assert NO_SELF_LOOPS_FLAG in result.details["argv"][VERIFY_LABEL]
    assert NO_SELF_LOOPS_FLAG not in result.details["argv"][VACUITY_LABEL]


def test_assertions_wrapped_in_a_branch_pass_only_with_the_narrowing_disclosed(tmp_path):
    result = ring_gate(tmp_path, "if_wrapped.c")
    assert result.outcome is Outcome.PASS, result.summary
    assert HARNESS_GAP in result.details["assumptions"]
    assert HARNESS_GAP in result.details["scope"]["known_gaps"]
    assert "narrowed further by its other code" in result.summary


@pytest.mark.parametrize("source", [None, "unreached_free.c"], ids=["no_free", "unreached_free"])
def test_a_harness_without_free_passes_whether_or_not_the_editable_code_references_free(tmp_path, source):
    result = below_gate(tmp_path, below_contract(TALLY_LOOP_LINE), source=source, harness=STACK_HARNESS)
    assert result.outcome is Outcome.PASS, result.summary
    assert DFCC_FREE_CRASH not in result.summary


def test_an_unreached_free_next_to_a_strlen_call_fails_closed_as_the_library_gap_states(tmp_path):
    contracts = below_contract(TALLY_LOOP_LINE)
    result = below_gate(tmp_path, contracts, source="unreached_free_strlen.c", harness=STACK_HARNESS)
    assert result.outcome is Outcome.INCONCLUSIVE and result.status is Status.UNKNOWN
    assert only_codes(result) == {Reason.INDUCTIVE_STEP_FAILED, Reason.UNWIND_INSUFFICIENT}
    assert failing(result, STRLEN_ASSIGNS) and failing(result, LIBRARY_UNWIND)
    assert never_counterexamples(result)


@pytest.mark.parametrize(
    "source, outcome", [("hooked.c", Outcome.ERROR), ("hooked_unreferenced.c", Outcome.PASS)], ids=["referenced", "unreferenced"]
)
def test_the_reserved_name_rule_checks_the_linked_program_where_unreferenced_statics_are_gone(tmp_path, source, outcome):
    result = below_gate(tmp_path, below_contract(HOOKED_LOOP_LINE), source=source)
    assert result.outcome is outcome, result.summary
    if outcome is Outcome.ERROR:
        assert TRUSTED_SYMBOL_RULES in result.summary and f"leanc_hook ({TALLY_SOURCE}:3)" in result.summary
