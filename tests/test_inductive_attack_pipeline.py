from __future__ import annotations

import pytest
from inductive_soundness_support import RING_PREDICATES, only_codes, ring_gate

from leanc.backends.inductive.scope import EDITABLE_FIRST_RULE
from leanc.backends.inductive.symbols import RENAME_REMEDY
from leanc.foundation.reasons import Reason
from leanc.foundation.status import Outcome, Status

pytestmark = pytest.mark.usefixtures("needs_cbmc")

HARNESS_RULES = "breaks the harness rules"
STRUCTURAL_RULES = "breaks its structural rules on some path"


def _is_harness_error(result, text: str) -> bool:
    return result.outcome is Outcome.ERROR and result.status is Status.TOOL_ERROR and text in result.summary


def test_a_verifier_assume_after_the_claimed_call_is_an_error_like_leanc_assume(tmp_path):
    result = ring_gate(tmp_path, "verifier_after.c")
    assert _is_harness_error(result, HARNESS_RULES), result.summary
    assert only_codes(result) == {Reason.TOOL_ERROR}
    assert "harness/inductive.c:16: __VERIFIER_assume after the call of the claimed function ring_add" in result.summary


def test_a_verifier_assume_in_a_helper_called_after_the_claimed_call_fails_the_dynamic_check(tmp_path):
    result = ring_gate(tmp_path, "verifier_helper_after.c")
    assert _is_harness_error(result, STRUCTURAL_RULES), result.summary
    assert "leanc_assume_after_claimed_call is FAILURE" in result.summary


def test_a_verifier_assume_before_the_claimed_call_is_listed_as_a_harness_assumption(tmp_path):
    result = ring_gate(tmp_path, "verifier_before.c")
    assert result.outcome is Outcome.PASS, result.summary
    assumptions = result.details["scope"]["harness_assumptions"]
    assert "harness/inductive.c:14: r->fill < 15" in assumptions, assumptions
    assert "3 harness assumption(s)" in result.summary


EDITABLE_FIRST = "leanc_editable_before_claimed_call:ring_add is FAILURE"
READS_FIXED_OBJECT = "reads objects the editable code fixes"


@pytest.mark.parametrize(
    "harness", ["predicate.c", "transitive.c", "hoisted.c", "side_effect.c", "pre_call.c", "argument.c"]
)
def test_editable_code_run_before_the_claimed_call_is_an_error(tmp_path, harness):
    result = ring_gate(tmp_path, harness, fixture=RING_PREDICATES)
    assert _is_harness_error(result, STRUCTURAL_RULES), result.summary
    assert EDITABLE_FIRST in result.summary


NO_ENTRY_CHECK = "some functions of the editable sources have no inserted entry check"


@pytest.mark.parametrize(
    "harness, source, where",
    [
        ("predicate.c", "src/ring_kr.c", "ring_ok() (src/ring.c:17)"),
        ("predicate.c", "src/ring_paren.c", "ring_ok() (src/ring.c:17)"),
        ("fix_first.c", "src/ring_fnret.c", "ring_fix() (src/ring.c:22)"),
    ],
    ids=["k_and_r_predicate", "parenthesised_name_predicate", "function_pointer_result_setup"],
)
def test_an_editable_function_the_entry_check_cannot_be_inserted_into_is_an_error(tmp_path, harness, source, where):
    result = ring_gate(tmp_path, harness, fixture=RING_PREDICATES, source=source)
    assert _is_harness_error(result, NO_ENTRY_CHECK), result.summary
    assert f"{where} is a function of the editable sources without the inserted entry check" in result.summary
    assert only_codes(result) == {Reason.TOOL_ERROR}


@pytest.mark.parametrize(
    "harness, where",
    [
        ("const_object.c", "harness/inductive.c:11: the harness reads ring_limit"),
        ("const_member.c", "harness/inductive.c:11: the harness reads ring_cfg"),
        ("const_initialiser.c", "the initialiser of limit_of_ring refers to ring_limit"),
    ],
)
def test_a_harness_that_reads_an_object_whose_value_the_editable_code_fixes_is_an_error(tmp_path, harness, where):
    result = ring_gate(tmp_path, harness, fixture=RING_PREDICATES)
    assert _is_harness_error(result, READS_FIXED_OBJECT), result.summary
    assert where in result.summary and "src/ring.c" in result.summary


def test_a_harness_that_includes_an_editable_source_is_an_error(tmp_path):
    result = ring_gate(tmp_path, "includes_editable.c", fixture=RING_PREDICATES)
    assert _is_harness_error(result, "includes editable code"), result.summary
    assert "includes the editable source src/ring.c" in result.summary


@pytest.mark.parametrize("harness", ["field.c", "mutable_object.c"])
def test_editable_calls_after_the_claimed_call_and_mutable_editable_objects_keep_the_bug_visible(tmp_path, harness):
    result = ring_gate(tmp_path, harness, fixture=RING_PREDICATES)
    assert result.outcome is Outcome.INCONCLUSIVE and result.status is Status.UNKNOWN, result.summary
    assert Reason.PROPERTY_UNPROVED in only_codes(result)


def test_a_static_editable_function_named_like_a_static_harness_function_stays_an_error_with_a_remedy(tmp_path):
    result = ring_gate(tmp_path, "static_twin.c", fixture=RING_PREDICATES)
    assert _is_harness_error(result, "breaks the trusted-symbol rules"), result.summary
    assert "ring_room$link1 (src/ring.c:" in result.summary and RENAME_REMEDY in result.summary


def test_the_scope_of_a_pass_states_that_editable_code_never_runs_before_the_claimed_call(tmp_path):
    result = ring_gate(tmp_path, "if_wrapped.c")
    assert result.outcome is Outcome.PASS, result.summary
    assert EDITABLE_FIRST_RULE in result.details["assumptions"]


TERMINATION_RULE = "ends the path or jumps out of it without a check"


@pytest.mark.parametrize(
    "harness, where",
    [
        ("underscore_exit_after.c", "harness/inductive.c:15: _Exit"),
        ("helper_abort.c", "harness/inductive.c:10: abort"),
        ("precall_abort.c", "harness/inductive.c:12: abort"),
    ],
    ids=["underscore_exit", "abort_in_a_helper", "abort_before_the_call"],
)
def test_every_silent_path_cut_of_the_harness_is_an_error(tmp_path, harness, where):
    result = ring_gate(tmp_path, harness)
    assert _is_harness_error(result, HARNESS_RULES), result.summary
    assert f"{where} {TERMINATION_RULE}" in result.summary


def test_a_spin_loop_without_a_later_probe_still_fails_its_unwinding_assertion(tmp_path):
    result = ring_gate(tmp_path, "while_no_probe.c")
    assert result.outcome is Outcome.INCONCLUSIVE and result.status is Status.UNKNOWN, result.summary
    assert Reason.UNWIND_INSUFFICIENT in only_codes(result)


@pytest.mark.parametrize("source", ["src/ring.c", "src/counter_reset.c"], ids=["honest", "counter_reset"])
def test_a_second_claimed_call_stays_an_error_even_when_the_editable_code_resets_the_counter(tmp_path, source):
    result = ring_gate(tmp_path, "twice.c", fixture=RING_PREDICATES, source=source)
    assert result.outcome is Outcome.ERROR and result.status is Status.TOOL_ERROR, result.summary
    if source == "src/counter_reset.c":
        assert "editable code uses names reserved for the inserted checks" in result.summary
        assert "src/ring.c:5: leanc_claimed_calls uses the leanc_ prefix reserved" in result.summary
