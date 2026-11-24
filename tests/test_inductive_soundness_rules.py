from __future__ import annotations

import json

import pytest
from inductive_build_support import TALLY, TALLY_HEADER, capture_context, tally_context
from inductive_soundness_support import (
    RING_FUNCTION,
    below_contract,
    below_gate,
    described,
    never_counterexamples,
    only_codes,
    ring_gate,
    shifted_tally_contracts,
)

from leanc.backends.inductive import run_inductive
from leanc.foundation.reasons import Reason
from leanc.foundation.status import Outcome, Status

pytestmark = pytest.mark.usefixtures("needs_cbmc")

TALLY_LOOP_LINE = 6
MACRO_TALLY_LOOP_LINE = 8
CAPTURE_MEMCPY_LOOP_LINE = 17
REJECTED_CONTRACTS = "the loop contracts in inductive/loops.json are rejected"
TRUSTED_SYMBOL_RULES = "breaks the trusted-symbol rules"
LINK_RENAME = "is a link-time rename of the trusted symbol"
SLOPPY_RULE = f"LEANC_ASSUME after the call of the claimed function {RING_FUNCTION}"
RING_INVARIANT_ASSERTION = "assertion r->fill < 16"
RING_PEEK_BOUND = "upper bound in r->data"
HEADER_RESET_MACRO = "#define TALLY_RESET (c = 0)\n"
HEADER_GUARD_END = "#endif\n"
CLAUSE_LIST_FIELDS = ("invariant", "assigns")


def _contract_with(field: str, text: str) -> str:
    value = [text] if field in CLAUSE_LIST_FIELDS else text
    return below_contract(TALLY_LOOP_LINE, **{field: value})


def _rejected_before_tools(result) -> bool:
    return (
        result.outcome is Outcome.INCONCLUSIVE
        and only_codes(result) == {Reason.CONTRACT_REJECTED}
        and result.invocations == []
        and result.details["argv"] == {}
    )


def test_a_loop_without_decreases_is_rejected_before_any_tool_runs(tmp_path):
    contracts = json.loads(below_contract(TALLY_LOOP_LINE))
    del contracts["loops"][0]["decreases"]
    result = below_gate(tmp_path, json.dumps(contracts))
    assert _rejected_before_tools(result), result.summary
    assert "decreases: Field required" in result.summary


@pytest.mark.parametrize(
    "field, text, construct",
    [
        ("invariant", "i++ <= n", "'++' is not allowed"),
        ("invariant", "(c = 0) == 0", "assignment is not allowed"),
        ("invariant", "c += 1", "assignment is not allowed"),
        ("invariant", "tally_below(a, n, x) >= 0", "function calls are not allowed"),
        ("assigns", "c++", "'++' is not allowed"),
        ("decreases", "n - i--", "'--' is not allowed"),
        ("decreases", "(i = n) - i", "assignment is not allowed"),
    ],
)
def test_a_side_effecting_clause_is_rejected_before_any_tool_runs(tmp_path, field, text, construct):
    result = below_gate(tmp_path, _contract_with(field, text))
    assert _rejected_before_tools(result), result.summary
    assert REJECTED_CONTRACTS in result.summary and construct in result.summary


def test_a_side_effect_hidden_in_an_editable_macro_is_rejected_before_any_tool_runs(tmp_path):
    contracts = below_contract(MACRO_TALLY_LOOP_LINE, invariant=["0 <= i", "RESET == 0"])
    result = below_gate(tmp_path, contracts, source="macro_reset.c")
    assert _rejected_before_tools(result), result.summary
    assert "RESET" in result.summary


def test_a_side_effect_hidden_in_a_protected_header_macro_never_reaches_the_clause(tmp_path):
    header = (TALLY / TALLY_HEADER).read_text(encoding="utf-8")
    assert header.endswith(HEADER_GUARD_END)
    header = header.removesuffix(HEADER_GUARD_END) + HEADER_RESET_MACRO + HEADER_GUARD_END
    contracts = below_contract(TALLY_LOOP_LINE, invariant=["0 <= i", "i <= n", "TALLY_RESET == 0"])
    result = below_gate(tmp_path, contracts, extra={TALLY_HEADER: header})
    assert result.outcome is Outcome.INCONCLUSIVE and only_codes(result) == {Reason.CONTRACT_REJECTED}
    assert "TALLY_RESET" in result.summary and "properties" not in result.details


@pytest.mark.parametrize(
    "source, shift, name",
    [("hooked.c", 2, "leanc_hook"), ("hooked_macro.c", 3, "leanc_hidden")],
)
def test_capture_by_an_editable_leanc_function_is_an_error(tmp_path, source, shift, name):
    ctx = tally_context(tmp_path, source=TALLY / "src" / source, contracts=shifted_tally_contracts(shift))
    result = run_inductive(ctx)
    assert result.outcome is Outcome.ERROR and result.status is Status.TOOL_ERROR
    assert only_codes(result) == {Reason.TOOL_ERROR}
    assert TRUSTED_SYMBOL_RULES in result.summary and f"{name} (src/tally.c:" in result.summary


def test_capture_of_a_trusted_harness_function_by_an_editable_static_is_an_error(tmp_path):
    result = run_inductive(capture_context(tmp_path))
    assert result.outcome is Outcome.ERROR and only_codes(result) == {Reason.TOOL_ERROR}
    assert f"helper$link1 (src/ed.c:3) {LINK_RENAME} helper" in result.summary


def test_capture_of_a_copy_model_by_a_macro_named_editable_static_is_an_error(tmp_path):
    result = below_gate(tmp_path, below_contract(CAPTURE_MEMCPY_LOOP_LINE), source="capture_memcpy.c")
    assert result.outcome is Outcome.ERROR and only_codes(result) == {Reason.TOOL_ERROR}
    assert f"memcpy$link1 (src/tally.c:6) {LINK_RENAME} memcpy" in result.summary


def test_a_sloppy_harness_assumption_after_the_claimed_call_is_an_error(tmp_path):
    result = ring_gate(tmp_path, "sloppy.c")
    assert result.outcome is Outcome.ERROR and result.status is Status.TOOL_ERROR
    assert only_codes(result) == {Reason.TOOL_ERROR}
    assert SLOPPY_RULE in result.summary


def test_the_careful_harness_exposes_the_bug_the_sloppy_assumption_hides(tmp_path):
    result = ring_gate(tmp_path, "careful.c")
    assert result.outcome is Outcome.INCONCLUSIVE and result.status is Status.UNKNOWN
    assert only_codes(result) == {Reason.PROPERTY_UNPROVED}
    assert [failure.location.function for failure in described(result, RING_INVARIANT_ASSERTION)] == ["main"]
    assert [failure.location.function for failure in described(result, RING_PEEK_BOUND)] == ["ring_peek"]
    assert never_counterexamples(result)
