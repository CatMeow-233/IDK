"""Tests for the CBMC gate (leanc.backends.cbmc).

Most tests run the real CBMC (``needs_cbmc``) on tiny projects assembled in a
temporary directory from ``tests/fixtures/cbmc``: one header, one editable
implementation (``src/impl.c``) and one trusted harness (``harness/h.c``).
A few tests use a fake ``cbmc`` (a shell script replaying captured output) to
exercise failure modes that real CBMC cannot produce on demand (running out
of memory, being killed by a signal).
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import stat
import typing
from pathlib import Path

import pytest

from leanc.backends import cbmc as cbmc_mod
from leanc.backends.cbmc import (
    CHECK_FLAGS,
    build_cbmc_argv,
    count_harness_assertions,
    coverage_by_file,
    find_probes,
    interpret_output,
    parse_cbmc_json,
    probe_occurrences,
    run_cbmc,
    strip_c_comments,
)
from leanc.backends.tools import Toolchain
from leanc.core.context import BackendContext, make_context
from leanc.core.status import Outcome, Status
from leanc.repair.counterexample import fingerprint
from leanc.specs.model import CheckName, Spec

FIX = Path(__file__).resolve().parent / "fixtures" / "cbmc"

FUNCTIONS = {
    "arr_get": "int arr_get(const int *arr, size_t len, size_t idx, int *out)",
    "avg": "int avg(int total, int count)",
    "add": "int add(int a, int b)",
    "deref": "int deref(const int *p)",
    "count_zeros": "size_t count_zeros(const int *a, size_t n)",
    "mul_div": "uint64_t mul_div(uint64_t a, uint64_t b)",
    "point_ratio": "int point_ratio(const struct point *p, int k)",
}


def make_cbmc_project(
    tmp_path: Path,
    *,
    src: str,
    harness: str,
    function: str,
    compile_defines: list[str] | None = None,
    **verification,
) -> BackendContext:
    """Assemble a project from fixtures and return a BackendContext for it."""
    root = tmp_path / "proj"
    for d in ("include", "src", "harness"):
        (root / d).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIX / "include" / "cbmc_fx.h", root / "include" / "cbmc_fx.h")
    shutil.copyfile(FIX / "src" / f"{src}.c", root / "src" / "impl.c")
    shutil.copyfile(FIX / "harness" / f"{harness}.c", root / "harness" / "h.c")
    data = {
        "spec_id": "cbmc-unit",
        "version": 1,
        "title": "cbmc unit test",
        "requirement": "test",
        "target": {"function": function, "signature": FUNCTIONS[function], "returns": {"type": "int"}},
        "sources": {"editable": ["src/impl.c"], "headers": ["include/cbmc_fx.h"], "include_dirs": ["include"]},
        "compile": {"defines": compile_defines or []},
        "verification": {"harness": "harness/h.c", "unwind": 5, "timeout_s": 120, **verification},
        "gates": ["compile", "cbmc"],
    }
    spec = Spec.model_validate(data)
    return make_context(spec, root, tmp_path / "run")


def _inputs(f) -> dict[str, str]:
    return {i.name: i.value for i in f.inputs}


def _by_class(res, cls: str):
    return [f for f in res.failures if f.kind == "counterexample" and f.property_id and f".{cls}." in f.property_id]


# --------------------------------------------------------------------------- argv (no CBMC needed)


def _fake_tools(ctx: BackendContext, cbmc: str | None = "/opt/cbmc/bin/cbmc") -> None:
    ctx.tools = Toolchain(clang=None, cbmc=cbmc, clang_version=None, cbmc_version="5.95.1 (cbmc-5.95.1)" if cbmc else None)


def test_check_flags_cover_every_spec_check():
    assert set(CHECK_FLAGS) == set(typing.get_args(CheckName))
    assert CHECK_FLAGS["bounds"] == "--bounds-check"
    assert CHECK_FLAGS["div-by-zero"] == "--div-by-zero-check"
    assert CHECK_FLAGS["memory-leak"] == "--memory-leak-check"
    assert all(f.startswith("--") and f.endswith("-check") for f in CHECK_FLAGS.values())


def test_build_argv_verify_mode(tmp_path):
    ctx = make_cbmc_project(
        tmp_path,
        src="arr_get_ok",
        harness="arr_get_harness",
        function="arr_get",
        compile_defines=["MAX=4", "-DFOO", "NDEBUG"],
        checks=["unsigned-overflow", "bounds"],
        unwind=7,
        unwindset={"arr_get.0": 3, "main.1": 12},
        malloc_may_fail=True,
        object_bits=12,
    )
    _fake_tools(ctx)
    argv = build_cbmc_argv(ctx, mode="verify")
    assert argv[:3] == ["/opt/cbmc/bin/cbmc", "src/impl.c", "harness/h.c"]
    assert "-Iinclude" in argv
    assert f"-I{os.path.abspath(ctx.include_dir)}" in argv
    assert "-DMAX=4" in argv and "-DFOO" in argv
    assert not any("NDEBUG" in a for a in argv), "NDEBUG would disable the harness assertions"
    flags = [a for a in argv if a.endswith("-check")]
    assert flags == [CHECK_FLAGS[c] for c in ctx.spec.verification.effective_checks()]
    assert flags[:6] == [
        "--bounds-check",
        "--pointer-check",
        "--div-by-zero-check",
        "--signed-overflow-check",
        "--pointer-overflow-check",
        "--undefined-shift-check",
    ]
    assert "--unsigned-overflow-check" in flags and flags.count("--bounds-check") == 1
    assert argv[argv.index("--unwind") + 1] == "7"
    assert argv[argv.index("--unwindset") + 1] == "arr_get.0:3,main.1:12"
    assert "--unwinding-assertions" in argv
    assert argv[argv.index("--malloc-may-fail") + 1] == "--malloc-fail-null"
    assert argv[argv.index("--object-bits") + 1] == "12"
    assert argv[-1] == "--json-ui"
    assert "-DLEANC_VACUITY_CHECK" not in argv and "--cover" not in argv


def test_build_argv_vacuity_and_coverage_modes(tmp_path):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    _fake_tools(ctx)
    vac = build_cbmc_argv(ctx, mode="vacuity")
    assert "-DLEANC_VACUITY_CHECK" in vac
    assert not [a for a in vac if a.endswith("-check")]
    assert "--unwinding-assertions" in vac and vac[-1] == "--json-ui"
    assert "--unwindset" not in vac and "--malloc-may-fail" not in vac and "--object-bits" not in vac
    cov = build_cbmc_argv(ctx, mode="coverage")
    assert cov[cov.index("--cover") + 1] == "location"
    # CBMC rejects --cover together with --unwinding-assertions.
    assert "--unwinding-assertions" not in cov
    assert "-DLEANC_VACUITY_CHECK" not in cov and not [a for a in cov if a.endswith("-check")]
    with pytest.raises(ValueError):
        build_cbmc_argv(ctx, mode="prove")  # type: ignore[arg-type]
    _fake_tools(ctx, cbmc=None)
    with pytest.raises(ValueError):
        build_cbmc_argv(ctx)


def test_find_probes_ignores_comments():
    text = (FIX / "harness" / "arr_get_harness.c").read_text()
    assert find_probes(text) == (True, ["after_call", "in_range", "out_of_range"])
    text = (FIX / "harness" / "arr_get_no_probe_harness.c").read_text()
    assert 'LEANC_REACH("in_comment")' in text
    assert find_probes(text) == (False, [])
    assert find_probes('// LEANC_REACH("x")\nint main(void){ return 0; }') == (False, [])
    code = 'const char *s = "/* not a comment */"; /* c */ LEANC_REACH( "a" ); LEANC_REACH("a"); LEANC_REACH(L);'
    assert find_probes(code) == (True, ["a"])
    assert "not a comment" in strip_c_comments(code) and "/* c */" not in strip_c_comments(code)


# --------------------------------------------------------------------------- real CBMC: verdicts


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
    # every failure of this buggy version shows idx == len
    for g in res.failures:
        gi = _inputs(g)
        assert gi["param idx"] == gi["param len"], (g.id, gi)
    rules = {d.rule_id for d in res.diagnostics}
    assert "pointer_dereference" in rules and "assertion" in rules
    assert all(d.tool == "cbmc" and d.severity == "error" for d in res.diagnostics)
    assert res.details["vacuity"]["status"] == "skipped"
    assert res.details["properties_failed"] == len(res.failures)
    assert len(res.invocations) == 1  # no vacuity run once real violations are known


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


def test_contradictory_assumptions_are_vacuous(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_contradictory_harness", function="arr_get")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    assert res.status is Status.UNKNOWN
    assert res.details["properties_failed"] == 0  # plain CBMC would say "VERIFICATION SUCCESSFUL"
    assert res.details["vacuity"]["status"] == "vacuous"
    assert res.details["vacuity"]["probes"] == {"after_call": False}
    (f,) = res.failures
    assert f.kind == "vacuity" and f.id == "cbmc:vacuity:after_call"
    assert f.location.file == "harness/h.c"
    assert f.raw_ref == "raw/cbmc-vacuity.json"
    assert "after_call" in res.summary and "vacuous" in res.summary
    assert [d.rule_id for d in res.diagnostics] == ["vacuity"]


def test_partially_vacuous_harness_names_the_probe(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_partial_vacuity_harness", function="arr_get")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE
    assert res.details["vacuity"]["probes"] == {"after_call": True, "huge_array": False}
    assert [f.id for f in res.failures] == ["cbmc:vacuity:huge_array"]
    assert "huge_array" in res.summary and "after_call" not in res.summary


def test_no_probes_is_inconclusive(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_no_probe_harness", function="arr_get")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE
    assert "no LEANC_REACH probes" in res.summary and "vacuity cannot be checked" in res.summary
    assert res.details["vacuity"]["status"] == "unchecked"
    assert len(res.invocations) == 1
    assert res.failures == []


def test_vacuity_check_disabled_is_inconclusive(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(
        tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get", vacuity_check=False
    )
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE
    assert "vacuity check disabled in spec; cannot exclude vacuous verification" in res.summary
    assert res.details["vacuity"]["status"] == "disabled"
    assert len(res.invocations) == 1


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
    assert res.failures == []  # spurious counterexamples on arbitrary return values are not reported
    assert any(d.rule_id == "no-body" and "arr_get" in d.message for d in res.diagnostics)


def test_struct_inputs_and_undefined_helper(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="point", harness="point_harness", function="point_ratio")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.FAIL
    assert any(d.rule_id == "no-body" and "helper_without_body" in d.message for d in res.diagnostics)
    assert any("helper_without_body" in a for a in res.details["assumptions"])
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
    assert res.outcome is Outcome.PASS, res.summary  # coverage is informational only
    cov = res.details["coverage"]
    assert set(cov) == {"src/impl.c"}
    st = cov["src/impl.c"]
    assert st["goals"] > 0 and 0 < st["covered"] < st["goals"]
    assert 8 in st["uncovered_lines"]  # the `return 0;` behind `n > 1000`
    assert 10 not in st["uncovered_lines"]
    assert any("coverage (non-blocking) src/impl.c" in n for n in res.details["notes"])
    assert set(res.details["argv"]) == {"verify", "vacuity", "coverage"}
    assert "--cover" in res.details["argv"]["coverage"]
    assert (ctx.raw_dir / "cbmc-coverage.json").is_file()
    assert len(res.invocations) == 3


def test_coverage_reveals_branch_the_harness_never_takes(tmp_path, needs_cbmc):
    # malloc never fails in CBMC's default model and `out` is always &out, so
    # the NULL-check branch of arr_get (line 6) is dead under this harness.
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get", coverage=True)
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.PASS
    st = res.details["coverage"]["src/impl.c"]
    assert st["covered"] == st["goals"] - 1
    assert st["uncovered_lines"] == [6]


def test_truncated_output_is_error(tmp_path, needs_cbmc, monkeypatch):
    monkeypatch.setattr(cbmc_mod, "MAX_OUTPUT_BYTES", 2000)
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR
    assert "truncated" in res.summary
    assert res.invocations[0].output_truncated


def test_example_safe_array_get_passes(example_copy, tmp_path, needs_cbmc):
    from leanc.specs.loader import load_spec

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
    from leanc.specs.loader import load_spec

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


# --------------------------------------------------------------------------- probe identity / harness integrity


def _patch_harness(ctx: BackendContext, old: str, new: str, count: int = 1) -> None:
    h = ctx.snapshot_dir / ctx.spec.verification.harness
    text = h.read_text()
    assert text.count(old) >= count, old
    h.write_text(text.replace(old, new, count))


def _example_with_harness_edits(example_copy, tmp_path, edits: list[tuple[str, str]]):
    from leanc.specs.loader import load_spec

    root = example_copy("safe_array_get")
    h = root / "harness" / "safe_array_get_harness.c"
    text = h.read_text()
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new)
    h.write_text(text)
    spec = load_spec(root / "spec.yaml")
    return run_cbmc(make_context(spec, root, tmp_path / "dup-run"))


_RELABEL = [
    ('LEANC_REACH("null_input")', 'LEANC_REACH("branch")'),
    ('LEANC_REACH("out_of_range")', 'LEANC_REACH("branch")'),
    ('LEANC_REACH("in_range")', 'LEANC_REACH("branch")'),
]
_OVERCONSTRAIN = [("LEANC_ASSUME(len <= MAX_LEN);", "LEANC_ASSUME(len <= MAX_LEN);\n    LEANC_ASSUME(idx >= len);")]


def test_duplicate_labels_cannot_hide_an_unreachable_probe(example_copy, tmp_path, needs_cbmc):
    # Regression: probes used to be keyed by label, so one reachable "branch"
    # probe masked the unreachable in-range "branch" probe -> VERIFIED_BOUNDED.
    res = _example_with_harness_edits(example_copy, tmp_path, _OVERCONSTRAIN + _RELABEL)
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    vac = res.details["vacuity"]
    assert vac["status"] == "vacuous"
    assert vac["duplicate_labels"] == {"branch": 3}
    assert vac["probes"] == {"after_call": True, "branch": False}
    reach = sorted(p["reachable"] for p in vac["properties"].values() if p["label"] == "branch")
    assert reach == [False, True, True]
    (f,) = res.failures
    assert f.kind == "vacuity" and f.id.startswith("cbmc:vacuity:branch@harness/safe_array_get_harness.c:")
    assert f.location.line == 43  # the in-range branch (one line lower than before the added assumption)
    assert "branch (harness/safe_array_get_harness.c:43)" in res.summary
    assert any("duplicate LEANC_REACH label" in n for n in res.details["notes"])


def test_unique_labels_flag_the_same_unreachable_branch(example_copy, tmp_path, needs_cbmc):
    res = _example_with_harness_edits(example_copy, tmp_path, _OVERCONSTRAIN)
    assert res.outcome is Outcome.INCONCLUSIVE
    assert [f.id for f in res.failures] == ["cbmc:vacuity:in_range"]


def test_duplicate_labels_all_reachable_pass_with_note(example_copy, tmp_path, needs_cbmc):
    res = _example_with_harness_edits(example_copy, tmp_path, _RELABEL)
    assert res.outcome is Outcome.PASS, res.summary
    vac = res.details["vacuity"]
    assert vac["status"] == "ok" and vac["duplicate_labels"] == {"branch": 3}
    assert len(vac["properties"]) == 4 and "all 4 vacuity probe(s) reachable" in res.summary
    assert any("duplicate LEANC_REACH label" in n for n in res.details["notes"])


def test_compiled_out_probe_counts_as_unreachable(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    _patch_harness(ctx, '    LEANC_REACH("after_call");\n', '    LEANC_REACH("after_call");\n#if 0\n    LEANC_REACH("ghost");\n#endif\n')
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    vac = res.details["vacuity"]
    assert vac["occurrences"] == 4 and len(vac["properties"]) == 3 and vac["unreported_occurrences"] == 1
    assert vac["probes"]["ghost"] is False
    (f,) = res.failures
    assert f.id == "cbmc:vacuity:ghost" and "not reported" in f.description
    assert "not reported by CBMC (ghost)" in res.summary


def test_partially_reported_duplicate_label_is_vacuous(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    _patch_harness(ctx, "    free(arr);\n", '#ifdef NEVER_DEFINED\n    LEANC_REACH("after_call");\n#endif\n    free(arr);\n')
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE
    (f,) = res.failures
    assert f.id == "cbmc:vacuity:after_call@unreported"
    assert f.description.startswith('1 of 2 occurrence(s) of LEANC_REACH("after_call")')


def test_unreported_probe_with_non_literal_label(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    _patch_harness(ctx, "    free(arr);\n", '#define GHOST_LABEL "ghost"\n#if 0\n    LEANC_REACH(GHOST_LABEL);\n#endif\n    free(arr);\n')
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE
    assert [f.id for f in res.failures] == ["cbmc:vacuity:unreported-probes"]
    assert "non-literal labels" in res.summary


def test_overridden_assert_machinery_is_error(tmp_path, needs_cbmc):
    # Without the override the wrong status code is a counterexample ...
    honest = make_cbmc_project(tmp_path / "honest", src="arr_get_wrong_code", harness="arr_get_harness", function="arr_get")
    res = run_cbmc(honest)
    assert res.outcome is Outcome.FAIL
    assert any(f.property_id.startswith("main.assertion.") for f in res.failures)
    # ... with __assert_fail overridden CBMC silently drops the harness assertions.
    cheat = make_cbmc_project(
        tmp_path / "cheat", src="arr_get_assert_override", harness="arr_get_harness", function="arr_get"
    )
    res = run_cbmc(cheat)
    assert res.outcome is Outcome.ERROR and res.status is Status.TOOL_ERROR, res.summary
    assert res.summary.startswith(
        "harness assertions missing from CBMC's property list (assert machinery overridden?)"
    )
    assert res.details["harness_assertions"] == {"in_source": 4, "cbmc_properties": 0}
    assert res.diagnostics[-1].rule_id == "missing-assertions"
    assert res.failures == [] and len(res.invocations) == 1


def test_count_harness_assertions_is_conservative():
    assert count_harness_assertions((FIX / "harness" / "arr_get_harness.c").read_text()) == 4
    code = """
#include <assert.h>
#define CHECK(x) assert(x)
#define LONG_CHECK(x) do { \\
    assert(x); \\
} while (0)
static void helper(int v) { assert(v > 0); }   /* never called: CBMC drops it */
int main(void)
{
    int a = nondet_int();
    assert(a != 1);              // counted
    __CPROVER_assert(a != 2, "x"); /* counted */
    /* assert(commented); */
    const char *s = "assert(in a string)";
    char c = '{';
    _Static_assert(sizeof(int) == 4, "int");
    static_assert(1, "c23");
    CHECK(a != 3);               /* macro: adds a property, not counted */
#if 0
    assert(compiled_out);
#else
    assert(maybe);
#endif
    if (a) { assert(a != 4); }   // counted
    (void)s; (void)c;
    return 0;
}
int after_main(void) { assert(0); return 0; }
"""
    assert count_harness_assertions(code) == 3
    assert count_harness_assertions("int f(void) { assert(1); return 0; }") == 0
    assert count_harness_assertions("int main(void);\nint main(void) { assert(1); }") == 1


def test_probe_occurrences():
    text = (FIX / "harness" / "arr_get_harness.c").read_text()
    assert probe_occurrences(text) == (3, {"after_call": 1, "in_range": 1, "out_of_range": 1})
    code = '/* LEANC_REACH("c") */ LEANC_REACH("a"); LEANC_REACH("a"); LEANC_REACH(L);'
    assert probe_occurrences(code) == (3, {"a": 2})


# --------------------------------------------------------------------------- fake CBMC: tool failures


def _fake_cbmc(tmp_path: Path, script: str, payload: str | None = None) -> str:
    d = tmp_path / "fakecbmc"
    d.mkdir(exist_ok=True)
    if payload is not None:
        (d / "payload.json").write_text(payload)
    exe = d / "cbmc"
    exe.write_text("#!/bin/sh\n" + script.replace("@PAYLOAD@", str(d / "payload.json")) + "\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(exe)


def test_out_of_memory_is_error(tmp_path):
    payload = (FIX / "json" / "mul_div_oom.verify.json").read_text()
    ctx = make_cbmc_project(tmp_path, src="mul_div", harness="mul_div_harness", function="mul_div")
    _fake_tools(ctx, cbmc=_fake_cbmc(tmp_path, "cat @PAYLOAD@\nexit 6", payload))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR, res.summary
    assert "ran out of memory" in res.summary and "memory_mb=4096" in res.summary
    assert "could not decide 2 property(ies)" in res.summary
    assert res.diagnostics and res.diagnostics[0].rule_id == "out-of-memory"
    assert res.failures == []


def test_out_of_memory_without_results_is_error(tmp_path):
    data = json.loads((FIX / "json" / "mul_div_oom.verify.json").read_text())
    data = [m for m in data if "result" not in m]
    ctx = make_cbmc_project(tmp_path, src="mul_div", harness="mul_div_harness", function="mul_div")
    _fake_tools(ctx, cbmc=_fake_cbmc(tmp_path, "cat @PAYLOAD@\nexit 6", json.dumps(data)))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR
    assert res.summary.startswith("CBMC (verify run) ran out of memory (verification.memory_mb=4096)")
    assert "SAT checker ran out of memory" in res.summary


def test_counterexample_found_before_running_out_of_memory_is_fail(tmp_path):
    data = json.loads((FIX / "json" / "mul_div_oom.verify.json").read_text())
    avg = json.loads((FIX / "json" / "avg.verify.json").read_text())
    prop_result = next(m["result"] for m in avg if "result" in m)[0]
    assert prop_result["status"] == "FAILURE"
    for m in data:
        if "result" in m:
            m["result"].append(prop_result)
    ctx = make_cbmc_project(tmp_path, src="mul_div", harness="mul_div_harness", function="mul_div")
    _fake_tools(ctx, cbmc=_fake_cbmc(tmp_path, "cat @PAYLOAD@\nexit 6", json.dumps(data)))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.FAIL
    assert [f.property_id for f in res.failures] == [prop_result["property"]]
    assert "2 property(ies) could not be decided (CBMC ran out of memory" in res.summary


def test_undecided_properties_are_error(tmp_path):
    data = json.loads((FIX / "json" / "mul_div_oom.verify.json").read_text())
    data = [m for m in data if m.get("messageText") != "SAT checker ran out of memory"]
    ctx = make_cbmc_project(tmp_path, src="mul_div", harness="mul_div_harness", function="mul_div")
    _fake_tools(ctx, cbmc=_fake_cbmc(tmp_path, "cat @PAYLOAD@\nexit 6", json.dumps(data)))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR
    assert "could not decide 2 property(ies)" in res.summary and "main.assertion.1 (ERROR)" in res.summary


def test_killed_by_signal_is_error(tmp_path):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    _fake_tools(ctx, cbmc=_fake_cbmc(tmp_path, 'echo \'[{"program": "CBMC 5.95.1 (cbmc-5.95.1)"}\'\nkill -SEGV $$'))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR
    assert "killed by SIGSEGV" in res.summary and "possibly out of memory" in res.summary


def test_garbage_output_is_error(tmp_path):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    _fake_tools(ctx, cbmc=_fake_cbmc(tmp_path, "echo 'this is not json'\necho 'boom' >&2\nexit 0"))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR
    assert "unparsable JSON" in res.summary and "boom" in res.summary


def _fake_cbmc_by_mode(tmp_path: Path, verify_payload: str, vacuity_action: str, vacuity_payload: str = "") -> str:
    """A fake cbmc that replays ``verify_payload`` for the verify run and runs
    ``vacuity_action`` (shell) for the -DLEANC_VACUITY_CHECK run."""
    d = tmp_path / "fakecbmc"
    d.mkdir(exist_ok=True)
    (d / "verify.json").write_text(verify_payload)
    (d / "vacuity.json").write_text(vacuity_payload)
    exe = d / "cbmc"
    exe.write_text(
        "#!/bin/sh\n"
        "VAC=\n"
        'for a in "$@"; do [ "$a" = "-DLEANC_VACUITY_CHECK" ] && VAC=1; done\n'
        f'if [ -n "$VAC" ]; then {vacuity_action.replace("@VACUITY@", str(d / "vacuity.json"))}; '
        f'else cat {d / "verify.json"}; fi\n'
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(exe)


def test_replayed_vacuity_outputs(tmp_path):
    ok = (FIX / "json" / "arr_get_ok.verify.json").read_text()
    for vac_file, outcome in (
        ("arr_get_ok.vacuity.json", Outcome.PASS),
        ("arr_get_contradictory.vacuity.json", Outcome.INCONCLUSIVE),
    ):
        sub = tmp_path / vac_file
        sub.mkdir()
        ctx = make_cbmc_project(sub, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
        vac = (FIX / "json" / vac_file).read_text()
        _fake_tools(ctx, cbmc=_fake_cbmc_by_mode(sub, ok, "cat @VACUITY@", vac))
        res = run_cbmc(ctx)
        assert res.outcome is outcome, (vac_file, res.summary)
    # contradictory replay against a harness declaring three probes: the two
    # probes CBMC never reported are unreachable too
    assert res.details["vacuity"]["probes"] == {"after_call": False, "in_range": False, "out_of_range": False}
    reported = {f.id: f.description for f in res.failures}
    assert "not reported by CBMC" in reported["cbmc:vacuity:in_range"]
    assert "not reported by CBMC" not in reported["cbmc:vacuity:after_call"]


def test_vacuity_run_timeout_is_timeout(tmp_path):
    ok = (FIX / "json" / "arr_get_ok.verify.json").read_text()
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get", timeout_s=1)
    _fake_tools(ctx, cbmc=_fake_cbmc_by_mode(tmp_path, ok, "exec sleep 30"))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.TIMEOUT and res.status is Status.TIMEOUT
    assert "vacuity run" in res.summary and "timed out" in res.summary
    assert [i.timed_out for i in res.invocations] == [False, True]
    assert res.details["vacuity"]["status"] == "skipped"


def test_vacuity_run_garbage_is_error(tmp_path):
    ok = (FIX / "json" / "arr_get_ok.verify.json").read_text()
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    _fake_tools(ctx, cbmc=_fake_cbmc_by_mode(tmp_path, ok, "echo nope"))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR
    assert "vacuity run" in res.summary and "unparsable JSON" in res.summary


def test_replayed_failure_output_is_fail(tmp_path):
    """The verdict logic only depends on CBMC's JSON: replaying a captured
    counterexample yields the same FAIL as the real run."""
    payload = (FIX / "json" / "arr_get_off_by_one.verify.json").read_text()
    ctx = make_cbmc_project(tmp_path, src="arr_get_off_by_one", harness="arr_get_harness", function="arr_get")
    _fake_tools(ctx, cbmc=_fake_cbmc(tmp_path, "cat @PAYLOAD@\nexit 10", payload))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.FAIL
    assert {f.property_id for f in res.failures} == {
        "main.assertion.3",
        "main.assertion.4",
        "arr_get.pointer_dereference.11",
    }


# --------------------------------------------------------------------------- output parsing units


def test_parse_cbmc_json_salvages_truncated_output():
    text = (FIX / "json" / "syntax_error.verify.json").read_text()
    msgs, err = parse_cbmc_json(text)
    assert err is None and msgs
    cut = text[: text.index("PARSING ERROR") + 20]
    msgs2, err2 = parse_cbmc_json(cut)
    assert err2 and "invalid JSON" in err2
    assert any("syntax error" in str(m.get("messageText", "")) for m in msgs2)
    assert parse_cbmc_json("") == ([], "empty output")
    assert parse_cbmc_json('{"a": 1}')[1].startswith("unexpected JSON")


def test_interpret_output_fields():
    out = interpret_output((FIX / "json" / "arr_get_ok.verify.json").read_text())
    assert out.parse_error is None and out.cprover_status == "success"
    assert out.program.startswith("CBMC 5.")
    assert out.platform and "64-bit" in out.platform
    assert out.results and all(r["status"] == "SUCCESS" for r in out.results)
    assert out.errors == []
    syn = interpret_output((FIX / "json" / "syntax_error.verify.json").read_text())
    assert syn.results is None and "PARSING ERROR" in syn.error_texts()


def test_coverage_by_file_on_captured_goals():
    out = interpret_output((FIX / "json" / "count_zeros_dead_code.coverage.json").read_text())
    assert out.goals
    cov = coverage_by_file(out.goals, ["src/impl.c", "src/other.c"])
    assert cov["src/other.c"] == {"goals": 0, "covered": 0, "uncovered_lines": []}
    st = cov["src/impl.c"]
    assert st["goals"] == len([g for g in out.goals if g["sourceLocation"]["file"] == "src/impl.c"])
    assert st["uncovered_lines"] == [8]


def test_details_are_json_serialisable(tmp_path):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    _fake_tools(ctx, cbmc=None)
    res = run_cbmc(ctx)
    json.dumps(res.model_dump(mode="json"))
    assert dataclasses.is_dataclass(ctx)
