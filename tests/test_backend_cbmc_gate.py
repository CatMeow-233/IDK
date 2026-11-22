from __future__ import annotations

import dataclasses
import json

from cbmc_support import _fake_tools, make_cbmc_project

from leanc.backends.cbmc import execution as cbmc_execution
from leanc.backends.cbmc import run_cbmc
from leanc.backends.counterexample import fingerprint
from leanc.foundation.status import Outcome, Status
from leanc.specs.loader import load_spec
from leanc.toolchain.context import make_context


def _inputs(f) -> dict[str, str]:
    return {i.name: i.value for i in f.inputs}


def _by_class(res, cls: str):
    return [f for f in res.failures if f.kind == "counterexample" and f.property_id and f".{cls}." in f.property_id]


def test_correct_code_passes_with_vacuity_ok(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    res = run_cbmc(ctx)
    assert res.stage == "cbmc"
    assert res.outcome is Outcome.PASS, res.summary
    assert res.status is None
    assert res.failures == []
    d = res.details
    assert d["vacuity"]["status"] == "ok"
    assert d["vacuity"]["probes"] == {"after_call": True, "in_range": True, "out_of_range": True}
    assert d["vacuity"]["occurrences"] == 3 and len(d["vacuity"]["properties"]) == 3
    assert all(p["reachable"] for p in d["vacuity"]["properties"].values())
    assert "duplicate_labels" not in d["vacuity"]
    assert d["harness_assertions"] == {"in_source": 4, "cbmc_properties": 4}
    assert d["properties_total"] > 0 and d["properties_passed"] == d["properties_total"]
    assert d["properties_failed"] == 0 and d["unwind_failures"] == []
    assert d["cbmc_version"] and "5." in d["cbmc_version"]
    assert set(d["argv"]) == {"verify", "vacuity"}
    assert d["checks"] == ctx.spec.verification.effective_checks()
    assert d["unwind"] == 5 and d["coverage"] is None
    for mode in ("verify", "vacuity"):
        raw = ctx.raw_dir / f"cbmc-{mode}.json"
        assert raw.is_file()
        json.loads(raw.read_text())
    assert [i.tool for i in res.invocations] == ["cbmc", "cbmc"]
    assert all(i.exit_code is not None and i.stdout_path for i in res.invocations)
    assert res.invocations[0].sandbox == ctx.sandbox.kind
    text = " ".join(d["assumptions"])
    assert "unwound at most 5" in text and "unwinding assertions" in text
    assert "inputs as constrained by the approved harness harness/h.c" in text
    assert "bounds" in text and "signed-overflow" in text
    assert "malloc model" in text and "non-vacuity" in text
    assert d["cbmc_version"] in text
    assert "all" in res.summary and "properties hold" in res.summary


def test_off_by_one_counterexample_shows_idx_equal_len(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="arr_get_off_by_one", harness="arr_get_harness", function="arr_get")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.FAIL, res.summary
    assert res.status is Status.COUNTEREXAMPLE_FOUND
    assert "counterexample" in res.summary
    oob = _by_class(res, "pointer_dereference")
    assert oob, [f.id for f in res.failures]
    f = oob[0]
    assert f.kind == "counterexample" and f.tool == "cbmc"
    assert f.id == f"cbmc:{f.property_id}"
    assert "outside object bounds" in f.description
    assert f.location.file == "src/impl.c" and f.location.line == 11 and f.location.function == "arr_get"
    ins = _inputs(f)
    assert ins["param idx"] == ins["param len"], ins
    assert ins["idx"] == ins["len"]
    assert int(ins["param len"]) <= 4
    assert f.call_stack[0].startswith("main (harness/h.c:")
    assert f.call_stack[-1] == "arr_get (src/impl.c:11)"
    assert f.raw_ref == "raw/cbmc-verify.json" and (ctx.run_dir / f.raw_ref).is_file()
    assert f.trace and f.trace[-1].kind == "failure"
    assert f.property_id in f.explanation and "src/impl.c:11" in f.explanation and "param idx" in f.explanation
    for g in res.failures:
        gi = _inputs(g)
        assert gi["param idx"] == gi["param len"], (g.id, gi)
    rules = {d.rule_id for d in res.diagnostics}
    assert "pointer_dereference" in rules and "assertion" in rules
    assert all(d.tool == "cbmc" and d.severity == "error" for d in res.diagnostics)
    assert res.details["vacuity"]["status"] == "skipped"
    assert res.details["properties_failed"] == len(res.failures)
    assert len(res.invocations) == 1


def test_division_by_zero(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="avg", harness="avg_harness", function="avg")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.FAIL
    (f,) = _by_class(res, "division-by-zero")
    assert f.description.startswith("division by zero")
    assert _inputs(f)["param count"] == "0"
    assert "count" in _inputs(f)
    assert [d.rule_id for d in res.diagnostics] == ["division-by-zero"]
    assert "division" in f.explanation.lower() and "zero" in f.explanation.lower()


def test_signed_overflow(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="add", harness="add_harness", function="add")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.FAIL
    (f,) = _by_class(res, "overflow")
    assert "arithmetic overflow on signed +" in f.description
    ins = _inputs(f)
    total = int(ins["param a"]) + int(ins["param b"])
    assert not (-(2**31) <= total < 2**31), ins
    assert res.diagnostics[0].rule_id == "overflow"
    assert "undefined behaviour" in f.explanation


def test_null_dereference(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="deref", harness="deref_harness", function="deref")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.FAIL
    null = [f for f in _by_class(res, "pointer_dereference") if "pointer NULL" in f.description]
    assert len(null) == 1
    f = null[0]
    assert _inputs(f)["param p"] == "NULL"
    assert _inputs(f)["p"] == "NULL"
    assert "NULL pointer is dereferenced" in f.explanation
    assert f.location.short() == "src/impl.c:5 in deref()"


def test_insufficient_unwind_is_inconclusive(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="count_zeros", harness="count_zeros_harness", function="count_zeros", unwind=3)
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    assert res.status is Status.UNKNOWN
    assert [f.kind for f in res.failures] == ["unwind"]
    f = res.failures[0]
    assert f.property_id == "count_zeros.unwind.0"
    assert f.location.file == "src/impl.c" and f.location.function == "count_zeros"
    assert int(_inputs(f)["n"]) >= 3
    assert "unwind bound 3" in f.explanation and "re-approved spec" in f.explanation
    assert "unwind bound 3 is insufficient" in res.summary
    assert "not sound" in res.summary and "re-approved spec" in res.summary
    assert res.details["unwind_failures"][0]["property"] == "count_zeros.unwind.0"
    assert res.details["properties_failed"] == 0
    assert res.details["vacuity"]["status"] == "ok"
    assert [d.rule_id for d in res.diagnostics] == ["unwind"]


def test_sufficient_unwind_passes(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="count_zeros", harness="count_zeros_harness", function="count_zeros", unwind=9)
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.PASS, res.summary
    assert res.details["unwinding_assertions_total"] >= 1


def test_real_failure_dominates_unwind_failure(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(
        tmp_path, src="count_zeros_off_by_one", harness="count_zeros_harness", function="count_zeros", unwind=3
    )
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.FAIL
    kinds = [f.kind for f in res.failures]
    assert "counterexample" in kinds and "unwind" in kinds
    assert "unwinding assertion(s) failed" in res.summary
    oob = _by_class(res, "pointer_dereference")
    assert oob and "outside object bounds" in oob[0].description


def test_timeout_on_hard_problem(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="mul_div", harness="mul_div_harness", function="mul_div", timeout_s=2)
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.TIMEOUT, res.summary
    assert res.status is Status.TIMEOUT
    assert "timed out after 2s" in res.summary
    assert res.invocations[0].timed_out
    assert res.duration_s < 30
    assert (ctx.raw_dir / "cbmc-verify.json").is_file()
    assert res.details["assumptions"]


def test_timeout_scale_applies(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="mul_div", harness="mul_div_harness", function="mul_div", timeout_s=4)
    ctx.options.timeout_scale = 0.25
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.TIMEOUT
    assert "timed out after 1s" in res.summary


def test_cbmc_missing_is_error(tmp_path):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    _fake_tools(ctx, cbmc=None)
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR and res.status is Status.TOOL_ERROR
    assert "cbmc executable not found" in res.summary
    assert res.invocations == []
    assert res.details["assumptions"]


def test_cbmc_unlaunchable_is_error(tmp_path):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    _fake_tools(ctx, cbmc=str(tmp_path / "no-such-dir" / "cbmc"))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR
    assert len(res.invocations) == 1


def test_missing_harness_is_error(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    (ctx.snapshot_dir / "harness" / "h.c").unlink()
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR
    assert "missing input file" in res.summary and "harness/h.c" in res.summary


def test_syntax_error_is_tool_error(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="syntax_error", harness="arr_get_harness", function="arr_get")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR and res.status is Status.TOOL_ERROR
    assert "PARSING ERROR" in res.summary
    assert "src/impl.c" in res.summary and "syntax error" in res.summary
    assert res.diagnostics and res.diagnostics[0].rule_id == "parsing-error"
    assert res.diagnostics[0].location.file == "src/impl.c"
    assert res.failures == []


def test_conversion_error_is_tool_error(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="undefined_symbol", harness="arr_get_harness", function="arr_get")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR
    assert "CONVERSION ERROR" in res.summary and "no_such_variable" in res.summary
    assert res.diagnostics[0].rule_id == "conversion-error"


def test_undefined_target_function_is_inconclusive(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="wrong_name", harness="arr_get_harness", function="arr_get")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    assert "has no definition" in res.summary
    assert res.failures == []
    assert any(d.rule_id == "no-body" and "arr_get" in d.message for d in res.diagnostics)
    details = res.details
    assert details["properties_failed"] == 0 and details["properties_unmodelled"] > 0
    assert details["properties_total"] == (
        details["properties_passed"] + details["properties_unmodelled"] + details["properties_unresolved"]
    )


def test_undefined_helper_makes_its_violation_inconclusive(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="point", harness="point_harness", function="point_ratio")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    assert res.status is Status.UNKNOWN
    assert "helper_without_body()" in res.summary
    assert any(d.rule_id == "no-body" and "helper_without_body" in d.message for d in res.diagnostics)
    assert any(d.rule_id == "unmodelled-callee" and "division-by-zero" in d.message for d in res.diagnostics)
    assert any("helper_without_body" in a for a in res.details["assumptions"])
    assert [f for f in res.failures if f.kind == "counterexample"] == []


def test_struct_inputs_render_in_counterexample(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="point_defined_helper", harness="point_harness", function="point_ratio")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.FAIL, res.summary
    (f,) = _by_class(res, "division-by-zero")
    ins = _inputs(f)
    assert ins["q"].startswith("{.x=5, .y=") and ".tag={'a'," in ins["q"]
    assert ins["arr"].startswith("{1, ") and ins["arr"].endswith(", 3}")
    assert ins["param p"] == "&q"
    assert ins["flag"] in ("TRUE", "FALSE")


def test_coverage_details(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(
        tmp_path,
        src="count_zeros_dead_code",
        harness="count_zeros_harness",
        function="count_zeros",
        unwind=9,
        coverage=True,
    )
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.PASS, res.summary
    cov = res.details["coverage"]
    assert set(cov) == {"src/impl.c"}
    st = cov["src/impl.c"]
    assert st["goals"] > 0 and 0 < st["covered"] < st["goals"]
    assert 8 in st["uncovered_lines"]
    assert 10 not in st["uncovered_lines"]
    assert any("coverage (non-blocking) src/impl.c" in n for n in res.details["notes"])
    assert set(res.details["argv"]) == {"verify", "vacuity", "coverage"}
    assert "--cover" in res.details["argv"]["coverage"]
    assert (ctx.raw_dir / "cbmc-coverage.json").is_file()
    assert len(res.invocations) == 3


def test_coverage_reveals_branch_the_harness_never_takes(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get", coverage=True)
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.PASS
    st = res.details["coverage"]["src/impl.c"]
    assert st["covered"] == st["goals"] - 1
    assert st["uncovered_lines"] == [6]


def test_truncated_output_is_error(tmp_path, needs_cbmc, monkeypatch):
    monkeypatch.setattr(cbmc_execution, "MAX_OUTPUT_BYTES", 2000)
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR
    assert "truncated" in res.summary
    assert res.invocations[0].output_truncated


def test_example_safe_array_get_passes(example_copy, tmp_path, needs_cbmc):
    root = example_copy("safe_array_get")
    spec = load_spec(root / "spec.yaml")
    ctx = make_context(spec, root, tmp_path / "example-run")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.PASS, res.summary
    assert res.details["vacuity"]["status"] == "ok"
    assert set(res.details["vacuity"]["probes"]) == {"after_call", "null_input", "out_of_range", "in_range"}
    assert "unsigned-overflow" in res.details["checks"] and "conversion" in res.details["checks"]
    assert any("0 <= len <= 8" in a for a in res.details["assumptions"])


def test_example_with_off_by_one_finds_idx_equal_len(example_copy, tmp_path, needs_cbmc):
    root = example_copy("safe_array_get")
    src = root / "src" / "safe_array.c"
    src.write_text(src.read_text().replace("idx >= len", "idx > len"))
    spec = load_spec(root / "spec.yaml")
    res = run_cbmc(make_context(spec, root, tmp_path / "example-run"))
    assert res.outcome is Outcome.FAIL
    for f in res.failures:
        ins = _inputs(f)
        assert ins["param idx"] == ins["param len"], (f.id, ins)
    fps = {fingerprint(f) for f in res.failures}
    assert len(fps) == len(res.failures)


def test_details_are_json_serialisable(tmp_path):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    _fake_tools(ctx, cbmc=None)
    res = run_cbmc(ctx)
    json.dumps(res.model_dump(mode="json"))
    assert dataclasses.is_dataclass(ctx)
