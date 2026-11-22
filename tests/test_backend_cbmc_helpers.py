from __future__ import annotations

import os
import typing

import pytest
from cbmc_support import FIX, _fake_tools, make_cbmc_project

from leanc.backends.cbmc import (
    CHECK_FLAGS,
    build_cbmc_argv,
    count_harness_assertions,
    find_probes,
    probe_occurrences,
    strip_c_comments,
)
from leanc.backends.termination import (
    trusted_tool_terminating_signal,
    untrusted_program_terminating_signal,
)
from leanc.foundation.runner import ProcResult
from leanc.specs.model import CheckName, SafetyProperty


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
    assert "--unwinding-assertions" not in cov
    assert "-DLEANC_VACUITY_CHECK" not in cov and not [a for a in cov if a.endswith("-check")]
    with pytest.raises(ValueError):
        build_cbmc_argv(ctx, mode="prove")
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


def test_unsandboxed_exit_status_128_plus_n_counts_as_killed():
    killed = ProcResult(argv=["cbmc"], cwd="/", exit_code=137, signal=None, timed_out=False,
                        output_truncated=False, duration_s=0.0, stdout="", stderr="", sandbox="none")
    assert trusted_tool_terminating_signal(killed) == 9
    assert untrusted_program_terminating_signal(killed) is None


def test_contract_safety_checks_reach_the_cbmc_verify_run(tmp_path):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    ctx.spec.contract.safety.append(SafetyProperty(id="SAFE9", text="no leaks", checks=["memory-leak"]))
    _fake_tools(ctx)
    assert "memory-leak" not in ctx.spec.verification.effective_checks()
    assert CHECK_FLAGS["memory-leak"] in build_cbmc_argv(ctx, mode="verify")
