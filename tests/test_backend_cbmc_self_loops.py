from __future__ import annotations

import pytest
from cbmc_support import make_cbmc_project

from leanc.backends.cbmc import run_cbmc
from leanc.backends.cbmc.argv import NO_SELF_LOOPS_TO_ASSUMPTIONS, UNWINDING_ASSERTIONS
from leanc.foundation.status import Outcome, Status

pytestmark = pytest.mark.usefixtures("needs_cbmc")

ENOUGH_UNWIND = 10
UNWINDING_PROPERTY = ".unwind."
SELF_LOOP_SOURCES = ("count_zeros_spin_for", "count_zeros_spin_while", "count_zeros_spin_goto", "count_zeros_spin_local")


def _gate(tmp_path, src: str):
    ctx = make_cbmc_project(
        tmp_path, src=src, harness="count_zeros_harness", function="count_zeros", unwind=ENOUGH_UNWIND
    )
    return run_cbmc(ctx)


def _unwinding_failures(result) -> list[str]:
    return [entry["property"] for entry in result.details["unwind_failures"] if UNWINDING_PROPERTY in entry["property"]]


def test_the_loop_free_control_passes_within_the_unwind_bound(tmp_path):
    result = _gate(tmp_path, "count_zeros")
    assert result.outcome is Outcome.PASS, result.summary


@pytest.mark.parametrize("src", [*SELF_LOOP_SOURCES, "count_zeros_spin_busy"])
def test_a_loop_that_never_ends_on_admitted_inputs_fails_its_unwinding_assertion(tmp_path, src):
    result = _gate(tmp_path, src)
    assert result.outcome is Outcome.INCONCLUSIVE and result.status is Status.UNKNOWN, result.summary
    assert _unwinding_failures(result), result.details["unwind_failures"]
    assert not [failure for failure in result.failures if failure.kind == "counterexample"]


@pytest.mark.parametrize("mode", ["verify", "vacuity"])
def test_both_verdict_runs_keep_self_loops_as_loops(tmp_path, mode):
    result = _gate(tmp_path, "count_zeros")
    argv = result.details["argv"][mode]
    assert argv.index(NO_SELF_LOOPS_TO_ASSUMPTIONS) == argv.index(UNWINDING_ASSERTIONS) + 1
