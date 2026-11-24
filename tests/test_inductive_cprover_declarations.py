from __future__ import annotations

import pytest
from inductive_soundness_support import only_codes, ring_gate

from leanc.backends.cbmc.symbol_table import CPROVER_DECLARATION_RULE
from leanc.foundation.reasons import Reason
from leanc.foundation.status import Outcome, Status

pytestmark = pytest.mark.usefixtures("needs_cbmc")

SHADOWING_HARNESSES = {
    "shadow_keep_param.c": "keep::__CPROVER_assert",
    "shadow_keep_local.c": "keep::1::__CPROVER_assert",
    "shadow_keep_function_param.c": "keep::__CPROVER_assert",
    "shadow_probe.c": "arm::__CPROVER_assert",
    "shadow_probe_local.c": "arm::1::__CPROVER_assert",
    "shadow_probe_function_param.c": "arm::__CPROVER_assert",
}


def test_the_unshadowed_helper_assumption_fails_the_dynamic_check(tmp_path):
    result = ring_gate(tmp_path, "keep_after.c")
    assert result.outcome is Outcome.ERROR and result.status is Status.TOOL_ERROR, result.summary
    assert "leanc_assume_after_claimed_call is FAILURE" in result.summary


@pytest.mark.parametrize("harness, symbol", sorted(SHADOWING_HARNESSES.items()), ids=lambda value: str(value))
def test_a_harness_that_declares_a_cprover_name_is_an_error(tmp_path, harness, symbol):
    result = ring_gate(tmp_path, harness)
    assert result.outcome is Outcome.ERROR and result.status is Status.TOOL_ERROR, result.summary
    assert only_codes(result) == {Reason.TOOL_ERROR}
    assert f"the linked verify program {CPROVER_DECLARATION_RULE}" in result.summary
    assert "harness/inductive.c:" in result.summary and symbol in result.summary
