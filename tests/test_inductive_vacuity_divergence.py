from __future__ import annotations

import pytest
from inductive_soundness_support import only_codes, ring_gate

from leanc.backends.cbmc.vacuity_divergence import DIVERGENCE_RULE
from leanc.foundation.reasons import Reason
from leanc.foundation.status import Outcome, Status

pytestmark = pytest.mark.usefixtures("needs_cbmc")

DIVERGING_HARNESSES = {"vacuity_define.c": "harness/inductive.c:13", "vacuity_stringize.c": "harness/inductive.c:16"}


@pytest.mark.parametrize("harness, where", sorted(DIVERGING_HARNESSES.items()), ids=["define", "stringize"])
def test_a_harness_that_runs_the_claimed_call_only_in_the_vacuity_build_is_an_error(tmp_path, harness, where):
    result = ring_gate(tmp_path, harness)
    assert result.outcome is Outcome.ERROR and result.status is Status.TOOL_ERROR, result.summary
    assert only_codes(result) == {Reason.TOOL_ERROR}
    assert f"the inductive harness harness/inductive.c {DIVERGENCE_RULE}: {where}:" in result.summary
