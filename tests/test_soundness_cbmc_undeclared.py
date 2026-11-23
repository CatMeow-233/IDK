from __future__ import annotations

import pytest
from cbmc_support import make_cbmc_project

from leanc.backends.cbmc import run_cbmc
from leanc.backends.cbmc.bodiless import undeclared_function
from leanc.foundation.status import Outcome, Status

UNDECLARED_INPUT = "nondet_ulonglong"
UNDECLARED_HARNESS = "mul_div_undeclared_nondet_harness"
DECLARED_HARNESS = "mul_div_declared_nondet_harness"
WIDE_BUG = "mul_div_wide_bug"
SMALL_BUG = "mul_div_small_bug"
IDENTITY = "mul_div_identity"
UNDECLARED_REASON = f"the code calls undeclared function(s) {UNDECLARED_INPUT}()"


def _run(tmp_path, *, src: str, harness: str):
    return run_cbmc(make_cbmc_project(tmp_path, src=src, harness=harness, function="mul_div"))


def _counterexamples(res) -> list:
    return [f for f in res.failures if f.kind == "counterexample"]


@pytest.mark.parametrize(
    "text, function",
    [
        (f"function '{UNDECLARED_INPUT}' is not declared", UNDECLARED_INPUT),
        ("function 'helper$1' is not declared", "helper$1"),
        ("no body for function nondet_int", None),
        ("function 'f' is declared", None),
    ],
)
def test_undeclared_warning_matcher(text, function):
    assert undeclared_function(text) == function


def test_undeclared_nondet_input_never_passes(tmp_path, needs_cbmc):
    res = _run(tmp_path, src=WIDE_BUG, harness=UNDECLARED_HARNESS)
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    assert res.status is Status.UNKNOWN
    assert res.summary.startswith("CBMC verdict inconclusive: " + UNDECLARED_REASON)
    assert res.details["undeclared_functions"] == [UNDECLARED_INPUT]
    flagged = [d for d in res.diagnostics if d.rule_id == "undeclared-function"]
    assert len(flagged) == 1 and flagged[0].severity == "warning"
    assert flagged[0].location is not None and flagged[0].location.line == 10


def test_undeclared_nondet_input_never_passes_correct_code(tmp_path, needs_cbmc):
    res = _run(tmp_path, src=IDENTITY, harness=UNDECLARED_HARNESS)
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    assert UNDECLARED_REASON in res.summary


def test_declared_nondet_input_reaches_the_wide_bug(tmp_path, needs_cbmc):
    res = _run(tmp_path, src=WIDE_BUG, harness=DECLARED_HARNESS)
    assert res.outcome is Outcome.FAIL, res.summary
    assert _counterexamples(res)
    assert res.details["undeclared_functions"] == []
    assert not any(d.rule_id == "undeclared-function" for d in res.diagnostics)


def test_declared_nondet_input_passes_correct_code(tmp_path, needs_cbmc):
    res = _run(tmp_path, src=IDENTITY, harness=DECLARED_HARNESS)
    assert res.outcome is Outcome.PASS, res.summary
    assert res.details["undeclared_functions"] == []


def test_counterexample_beside_undeclared_input_stays_a_counterexample(tmp_path, needs_cbmc):
    res = _run(tmp_path, src=SMALL_BUG, harness=UNDECLARED_HARNESS)
    assert res.outcome is Outcome.FAIL, res.summary
    assert _counterexamples(res)
    assert f"In addition, {UNDECLARED_REASON}" in res.summary
