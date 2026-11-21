from __future__ import annotations

import copy

import pytest
from cbmc_replay_support import fixture_messages, replaying_cbmc
from cbmc_support import _fake_tools, make_cbmc_project

from leanc.backends.cbmc import run_cbmc
from leanc.backends.cbmc.bodiless import (
    is_modelled_bodiless,
    split_violations,
    unmodelled_return_values_in_trace,
)
from leanc.foundation.status import Outcome, Status

UNMODELLED = "unmodelled_helper"
NO_BODY_WARNING_ANCHOR = "Generic Property Instrumentation"
FAILING_PAYLOAD = "arr_get_off_by_one.verify.json"
PASSING_PAYLOAD = "arr_get_ok.verify.json"
VACUITY_PAYLOAD = "arr_get_ok.vacuity.json"
FAILING_PROPERTIES = ("main.assertion.3", "main.assertion.4", "arr_get.pointer_dereference.11")


def _no_body_warning(function: str) -> dict:
    return {"messageText": f"**** WARNING: no body for function {function}", "messageType": "WARNING"}


def _with_warnings(messages: list[dict], functions: list[str]) -> list[dict]:
    anchor = next(i for i, m in enumerate(messages) if m.get("messageText") == NO_BODY_WARNING_ANCHOR)
    return messages[: anchor + 1] + [_no_body_warning(f) for f in functions] + messages[anchor + 1 :]


def _call_step(function: str) -> dict:
    return {"stepType": "function-call", "hidden": False, "function": {"identifier": function, "displayName": function}}


def _return_step(lhs: str) -> dict:
    return {"stepType": "assignment", "assignmentType": "variable", "hidden": False, "lhs": lhs, "value": {"data": "1"}}


def _inject(messages: list[dict], steps_by_property: dict[str, list[dict]]) -> list[dict]:
    messages = copy.deepcopy(messages)
    for message in messages:
        for result in message.get("result", []):
            injected = steps_by_property.get(result["property"])
            if injected:
                result["trace"] = result["trace"][:-1] + injected + result["trace"][-1:]
    return messages


def _run(tmp_path, verify: list[dict], *, src: str) -> object:
    ctx = make_cbmc_project(tmp_path, src=src, harness="arr_get_harness", function="arr_get")
    _fake_tools(ctx, cbmc=replaying_cbmc(tmp_path / "fake", verify, fixture_messages(VACUITY_PAYLOAD)))
    return run_cbmc(ctx)


def _counterexamples(res) -> list:
    return [f for f in res.failures if f.kind == "counterexample"]


@pytest.mark.parametrize(
    "function, modelled",
    [
        ("nondet_int", True),
        ("nondet_anything", True),
        ("__CPROVER_assume", True),
        ("__builtin_prefetch", True),
        ("__builtin_expect", True),
        ("__builtin_expect_with_probability", False),
        ("__builtin_ia32_pshufd256", False),
        ("__builtin_ia32_maskstored256", False),
        ("helper_without_body", False),
        ("my_nondet_int", False),
    ],
)
def test_bodiless_allowlist(function, modelled):
    assert is_modelled_bodiless(function) is modelled


def test_trace_matcher_finds_return_temporaries_with_identifier_boundary():
    unmodelled = frozenset({"ext", "__builtin_ia32_pshufd256"})
    trace = [
        _return_step("return_value_ext_other"),
        _return_step("return_value_nondet_int$0"),
        _call_step("ext_other"),
        _call_step("ext"),
    ]
    assert unmodelled_return_values_in_trace({"trace": trace}, unmodelled) == []
    for step in (
        _return_step("return_value_ext"),
        _return_step("return_value_ext$0"),
        _return_step("goto_symex$$return_value$$ext"),
    ):
        assert unmodelled_return_values_in_trace({"trace": [*trace, step]}, unmodelled) == ["ext"], step
    shuffle = [_return_step("return_value___builtin_ia32_pshufd256[3l]")]
    assert unmodelled_return_values_in_trace({"trace": shuffle}, unmodelled) == ["__builtin_ia32_pshufd256"]
    assert unmodelled_return_values_in_trace({"trace": "garbage"}, unmodelled) == []


def test_split_violations_keeps_traces_that_ignore_unmodelled_results_real():
    dependent = {"property": "p.1", "trace": [_call_step("ext"), _return_step("return_value_ext")]}
    real = {"property": "p.2", "trace": [_call_step("ext"), _call_step("defined_function")]}
    split = split_violations([dependent, real], frozenset({"ext"}))
    assert split.real == [real]
    assert [(item.result["property"], item.functions) for item in split.dependent] == [("p.1", ("ext",))]


def test_violation_using_an_unmodelled_result_is_not_a_counterexample(tmp_path):
    steps = {prop: [_call_step(UNMODELLED), _return_step(f"return_value_{UNMODELLED}")] for prop in FAILING_PROPERTIES}
    verify = _inject(_with_warnings(fixture_messages(FAILING_PAYLOAD), [UNMODELLED]), steps)
    res = _run(tmp_path, verify, src="arr_get_off_by_one")
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    assert res.status is Status.UNKNOWN
    assert _counterexamples(res) == []
    assert f"{UNMODELLED}()" in res.summary and "not counterexamples" in res.summary
    unmodelled = [d for d in res.diagnostics if d.rule_id == "unmodelled-callee"]
    assert len(unmodelled) == len(FAILING_PROPERTIES)
    assert all(d.severity == "warning" and UNMODELLED in d.message for d in unmodelled)
    assert res.details["properties_failed"] == 0
    assert res.details["properties_unmodelled"] == len(FAILING_PROPERTIES)
    assert {v["property"] for v in res.details["unmodelled_violations"]} == set(FAILING_PROPERTIES)
    assert res.details["bodiless_functions"] == {"modelled": [], "unmodelled": [UNMODELLED]}


def test_return_temporary_alone_marks_violation_dependent(tmp_path):
    steps = {prop: [_return_step(f"return_value_{UNMODELLED}$0")] for prop in FAILING_PROPERTIES}
    verify = _inject(_with_warnings(fixture_messages(FAILING_PAYLOAD), [UNMODELLED]), steps)
    res = _run(tmp_path, verify, src="arr_get_off_by_one")
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    assert _counterexamples(res) == []


def test_discarded_unmodelled_call_keeps_the_counterexamples(tmp_path):
    steps = {prop: [_call_step(UNMODELLED)] for prop in FAILING_PROPERTIES}
    verify = _inject(_with_warnings(fixture_messages(FAILING_PAYLOAD), [UNMODELLED]), steps)
    res = _run(tmp_path, verify, src="arr_get_off_by_one")
    assert res.outcome is Outcome.FAIL and res.status is Status.COUNTEREXAMPLE_FOUND, res.summary
    assert {f.property_id for f in _counterexamples(res)} == set(FAILING_PROPERTIES)
    assert res.details["properties_unmodelled"] == 0


def test_mutation_real_violation_still_fails_beside_unmodelled_callee(tmp_path):
    steps = {FAILING_PROPERTIES[0]: [_return_step(f"return_value_{UNMODELLED}$0")]}
    verify = _inject(_with_warnings(fixture_messages(FAILING_PAYLOAD), [UNMODELLED]), steps)
    res = _run(tmp_path, verify, src="arr_get_off_by_one")
    assert res.outcome is Outcome.FAIL, res.summary
    assert res.status is Status.COUNTEREXAMPLE_FOUND
    assert {f.property_id for f in _counterexamples(res)} == set(FAILING_PROPERTIES[1:])
    assert res.details["properties_failed"] == 2 and res.details["properties_unmodelled"] == 1
    assert f"{UNMODELLED}()" in res.summary and FAILING_PROPERTIES[0] in res.summary


def test_never_pass_with_unmodelled_callee(tmp_path):
    verify = _with_warnings(fixture_messages(PASSING_PAYLOAD), [UNMODELLED])
    res = _run(tmp_path, verify, src="arr_get_ok")
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    assert res.status is Status.UNKNOWN
    assert res.summary.startswith("CBMC verdict inconclusive: the code calls body-less function(s)")
    assert any("NOT modelled by CBMC" in a and UNMODELLED in a for a in res.details["assumptions"])
    assert res.details["vacuity"]["status"] == "ok"


def test_modelled_bodiless_functions_keep_the_pass(tmp_path):
    modelled = ["__builtin_expect", "__builtin_prefetch", "nondet_extra"]
    verify = _with_warnings(fixture_messages(PASSING_PAYLOAD), modelled)
    res = _run(tmp_path, verify, src="arr_get_ok")
    assert res.outcome is Outcome.PASS, res.summary
    assert res.details["bodiless_functions"] == {"modelled": modelled, "unmodelled": []}
    assert {d.severity for d in res.diagnostics if d.rule_id == "no-body"} == {"note"}
    assert not any("NOT modelled" in a for a in res.details["assumptions"])


def test_replay_without_bodiless_functions_is_unchanged(tmp_path):
    res = _run(tmp_path / "ok", fixture_messages(PASSING_PAYLOAD), src="arr_get_ok")
    assert res.outcome is Outcome.PASS, res.summary
    assert res.details["bodiless_functions"] == {"modelled": [], "unmodelled": []}
    failing = _run(tmp_path / "bad", fixture_messages(FAILING_PAYLOAD), src="arr_get_off_by_one")
    assert failing.outcome is Outcome.FAIL
    assert {f.property_id for f in _counterexamples(failing)} == set(FAILING_PROPERTIES)
    assert not any(d.rule_id == "unmodelled-callee" for d in failing.diagnostics)
