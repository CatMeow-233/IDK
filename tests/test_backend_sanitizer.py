from __future__ import annotations

import os
import textwrap
import time
from pathlib import Path

import pytest

from leanc.backends.sanitizer import (
    LIMITATION,
    build_command,
    find_llvm_symbolizer,
    parse_build_diagnostics,
    parse_sanitizer_findings,
    parse_sanitizer_output,
    run_sanitizer_tests,
    sanitizer_env,
    sanitizer_plan,
    scan_sanitizer_control,
    ubsan_rule,
)
from leanc.foundation.sandbox import Sandbox
from leanc.foundation.status import Outcome, Status
from leanc.specs.loader import load_spec
from leanc.specs.model import Spec
from leanc.toolchain.context import VerifyOptions, make_context

HEADER = "#ifndef F_H\n#define F_H\nint f(int n);\n#endif\n"

DRIVER = textwrap.dedent(
    """\
    #include <stdio.h>
    #include <stdlib.h>
    #include "f.h"

    int main(int argc, char **argv)
    {
        int n = argc > 1 ? atoi(argv[1]) : 0;
        int r = f(n);
        printf("r=%d\\n", r);
        return r == 42 ? 0 : 1;
    }
    """
)

GOOD_SRC = '#include "f.h"\n\nint f(int n)\n{\n    return n + 32;\n}\n'


def make_spec(cases: list[dict], **tests_kw) -> Spec:
    return Spec.model_validate(
        {
            "spec_id": "san-test",
            "version": 1,
            "title": "sanitizer backend test",
            "requirement": "f(10) == 42",
            "target": {"function": "f", "signature": "int f(int n)", "returns": {"type": "int"}},
            "sources": {"editable": ["src/f.c"], "headers": ["include/f.h"], "include_dirs": ["include"]},
            "tests": {"driver": "tests/driver.c", "cases": cases, **tests_kw},
            "verification": {"harness": "harness/f_harness.c"},
            "gates": ["compile", "sanitizer"],
        }
    )


def make_project(root: Path, src: str, driver: str = DRIVER) -> Path:
    for d in ("src", "include", "tests"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "include" / "f.h").write_text(HEADER)
    (root / "src" / "f.c").write_text(src)
    (root / "tests" / "driver.c").write_text(driver)
    return root


def run_gate(tmp_path: Path, src: str, cases: list[dict], *, driver: str = DRIVER, sandbox: Sandbox | None = None,
             options: VerifyOptions | None = None, **tests_kw):
    root = make_project(tmp_path / "proj", src, driver)
    spec = make_spec(cases, **tests_kw)
    ctx = make_context(spec, root, root / ".leanc" / "runs" / "r1", options or VerifyOptions())
    if sandbox is not None:
        ctx.sandbox = sandbox
    res = run_sanitizer_tests(ctx)
    return ctx, res


def line_of(src: str, needle: str) -> int:
    for i, line in enumerate(src.splitlines(), 1):
        if needle in line:
            return i
    raise AssertionError(needle)


def test_passing_program(tmp_path, needs_clang, needs_bwrap):
    ctx, res = run_gate(tmp_path, GOOD_SRC, [{"name": "ok", "args": ["10"], "expect_stdout": "r=42\n"}])
    assert res.stage == "sanitizer"
    assert res.outcome is Outcome.PASS, res.summary
    assert res.status is None
    assert res.failures == [] and res.diagnostics == []
    d = res.details
    assert d["limitation"] == LIMITATION
    assert d["sanitizers"] == ["address", "undefined", "leak"]
    assert d["sandbox"] == "bwrap" and "sandbox_warning" not in d
    assert "-fsanitize=address,undefined" in d["build"]["argv"]
    assert "-fno-sanitize-recover=all" in d["build"]["argv"]
    assert "src/f.c" in d["build"]["argv"] and "tests/driver.c" in d["build"]["argv"]
    case = d["cases"][0]
    assert case["name"] == "ok" and case["args"] == ["10"]
    assert case["exit_code"] == 0 and case["expected_exit"] == 0
    assert case["timed_out"] is False and case["findings"] == [] and case["stdout_matches"] is True
    assert case["status"] == "pass" and case["duration_s"] >= 0
    assert "detect_leaks=1" in d["env"]["ASAN_OPTIONS"]
    assert d["counts"] == {"cases": 1, "passed": 1, "failed": 0, "timed_out": 0, "errors": 0, "findings": 0}
    assert [i.tool for i in res.invocations] == ["clang", "sanitizer-test"]
    assert res.invocations[1].sandbox == "bwrap"
    assert (ctx.run_dir / res.invocations[1].stdout_path).read_text() == "r=42\n"


HBO_SRC = textwrap.dedent(
    """\
    #include <stdlib.h>
    #include "f.h"

    int f(int n)
    {
        int *p = malloc((size_t)n * sizeof(int));
        if (!p) return -1;
        for (int i = 0; i < n; i++) p[i] = i;
        int v = p[n]; /* one past the end */
        free(p);
        return v;
    }
    """
)


def test_heap_buffer_overflow_located_by_asan(tmp_path, needs_clang, needs_bwrap):
    ctx, res = run_gate(tmp_path, HBO_SRC, [{"name": "oob", "args": ["4"]}])
    assert res.outcome is Outcome.FAIL
    assert res.status is Status.RUNTIME_TEST_FAILED
    asan = [d for d in res.diagnostics if d.tool == "asan"]
    assert len(asan) == 1, res.diagnostics
    d = asan[0]
    assert d.rule_id == "heap-buffer-overflow"
    assert d.location.file == "src/f.c"
    assert d.location.line == line_of(HBO_SRC, "one past the end")
    assert d.location.function == "f"
    assert "READ of size 4" in d.message
    assert "heap-buffer-overflow" in d.raw
    fr = next(f for f in res.failures if f.tool == "asan")
    assert fr.kind == "runtime" and fr.property_id == "heap-buffer-overflow"
    assert {i.name: i.value for i in fr.inputs} == {"test_case": "oob", "argv": "4"}
    assert fr.call_stack and fr.call_stack[0].startswith("#0 f src/f.c:")
    assert fr.raw_ref and (ctx.run_dir / fr.raw_ref).is_file()
    assert "heap-buffer-overflow" in (ctx.run_dir / fr.raw_ref).read_text()
    assert not [d for d in res.diagnostics if d.tool == "test-oracle"]
    assert res.details["cases"][0]["findings"][0]["rule_id"] == "heap-buffer-overflow"
    assert "oob" in res.summary and "heap-buffer-overflow" in res.summary


UAF_SRC = textwrap.dedent(
    """\
    #include <stdlib.h>
    #include "f.h"

    int f(int n)
    {
        int *p = malloc(4 * sizeof(int));
        if (!p) return -1;
        p[0] = n;
        free(p);
        return p[0] + 32; /* use after free */
    }
    """
)


def test_heap_use_after_free(tmp_path, needs_clang, needs_bwrap):
    _, res = run_gate(tmp_path, UAF_SRC, [{"name": "uaf", "args": ["10"]}])
    assert res.outcome is Outcome.FAIL
    d = next(d for d in res.diagnostics if d.tool == "asan")
    assert d.rule_id == "heap-use-after-free"
    assert (d.location.file, d.location.line) == ("src/f.c", line_of(UAF_SRC, "use after free"))


LEAK_SRC = textwrap.dedent(
    """\
    #include <stdlib.h>
    #include <string.h>
    #include "f.h"

    char *volatile sink;

    int f(int n)
    {
        char *buf = malloc(64); /* leaked */
        if (!buf) return -1;
        memset(buf, n, 64);
        sink = buf;
        sink = NULL;
        return 42;
    }
    """
)


def test_memory_leak_reported_by_lsan(tmp_path, needs_clang, needs_bwrap):
    _, res = run_gate(
        tmp_path, LEAK_SRC, [{"name": "leaky", "args": ["1"], "expect_stdout": "r=42\n"}]
    )
    assert res.outcome is Outcome.FAIL
    leaks = [d for d in res.diagnostics if d.tool == "lsan"]
    assert len(leaks) == 1, res.diagnostics
    d = leaks[0]
    assert d.rule_id == "memory-leak"
    assert "64 byte(s)" in d.message and "Direct leak" in d.message
    assert (d.location.file, d.location.line) == ("src/f.c", line_of(LEAK_SRC, "leaked"))
    fr = next(f for f in res.failures if f.tool == "lsan")
    assert fr.kind == "runtime"
    assert res.details["cases"][0]["stdout_matches"] is None
    assert not [d for d in res.diagnostics if d.tool == "test-oracle"]


def test_leak_detection_disabled_when_not_requested(tmp_path, needs_clang, needs_bwrap):
    _, res = run_gate(tmp_path, LEAK_SRC, [{"name": "leaky", "args": ["1"]}], sanitizers=["address", "undefined"])
    assert res.outcome is Outcome.PASS, res.summary
    assert "detect_leaks=0" in res.details["env"]["ASAN_OPTIONS"]
    assert res.details["sanitizers"] == ["address", "undefined"]


OVF_SRC = textwrap.dedent(
    """\
    #include "f.h"

    int f(int n)
    {
        int big = 2147483600;
        return big + n; /* overflows for n > 47 */
    }
    """
)


def test_signed_overflow_reported_by_ubsan(tmp_path, needs_clang, needs_bwrap):
    _, res = run_gate(tmp_path, OVF_SRC, [{"name": "ovf", "args": ["100"]}])
    assert res.outcome is Outcome.FAIL
    d = next(d for d in res.diagnostics if d.tool == "ubsan")
    assert d.rule_id == "signed-integer-overflow"
    assert (d.location.file, d.location.line) == ("src/f.c", line_of(OVF_SRC, "overflows"))
    assert d.location.function == "f"
    assert "cannot be represented in type 'int'" in d.message
    fr = next(f for f in res.failures if f.tool == "ubsan")
    assert fr.kind == "runtime" and fr.property_id == "signed-integer-overflow"


def test_wrong_exit_code_is_test_oracle_failure(tmp_path, needs_clang, needs_bwrap):
    src = '#include "f.h"\nint f(int n) { return n + 31; }\n'
    _, res = run_gate(tmp_path, src, [{"name": "off_by_one", "args": ["10"]}])
    assert res.outcome is Outcome.FAIL
    assert res.status is Status.RUNTIME_TEST_FAILED
    d = next(d for d in res.diagnostics if d.tool == "test-oracle")
    assert d.rule_id == "exit-code" and "exit code 1" in d.message and "expected 0" in d.message
    fr = next(f for f in res.failures if f.tool == "test-oracle")
    assert fr.kind == "test-oracle"
    assert {i.name: i.value for i in fr.inputs}["test_case"] == "off_by_one"
    assert res.details["cases"][0]["exit_code"] == 1


def test_expected_stdout_mismatch(tmp_path, needs_clang, needs_bwrap):
    _, res = run_gate(tmp_path, GOOD_SRC, [{"name": "out", "args": ["10"], "expect_stdout": "r=43\n"}])
    assert res.outcome is Outcome.FAIL
    diags = [d for d in res.diagnostics if d.tool == "test-oracle"]
    assert [d.rule_id for d in diags] == ["stdout"]
    assert "line 1" in diags[0].message and "'r=43'" in diags[0].message and "'r=42'" in diags[0].message
    assert res.details["cases"][0]["stdout_matches"] is False


LOOP_SRC = textwrap.dedent(
    """\
    #include "f.h"

    int f(int n)
    {
        volatile int x = n;
        while (x >= 0)
            x = (x + 1) & 0xff;
        return 42;
    }
    """
)


def test_infinite_loop_times_out(tmp_path, needs_clang, needs_bwrap):
    t0 = time.monotonic()
    _, res = run_gate(
        tmp_path, LOOP_SRC, [{"name": "ok", "args": ["10"]}, {"name": "spin", "args": ["1"], "timeout_s": 1}],
        timeout_s=1,
    )
    assert res.outcome is Outcome.TIMEOUT, res.summary
    assert res.status is Status.TIMEOUT
    spin = next(c for c in res.details["cases"] if c["name"] == "spin")
    assert spin["timed_out"] is True and spin["status"] == "timeout"
    assert spin["duration_s"] < 5
    assert "spin" in res.summary
    fr = next(f for f in res.failures if f.kind == "timeout")
    assert fr.tool == "test-oracle"
    assert time.monotonic() - t0 < 30


def test_cpu_limit_is_reported_as_timeout(tmp_path, needs_clang, needs_bwrap):
    _, res = run_gate(tmp_path, LOOP_SRC, [{"name": "spin", "args": ["1"], "timeout_s": 2}])
    assert res.outcome is Outcome.TIMEOUT
    assert res.details["cases"][0]["timeout_reason"] in ("CPU time limit", "wall-clock timeout")


def test_link_failure_is_compile_error(tmp_path, needs_clang, needs_bwrap):
    src = '#include "f.h"\nint helper(int n);\n\nint f(int n)\n{\n    return helper(n);\n}\n'
    _, res = run_gate(tmp_path, src, [{"name": "ok", "args": ["10"]}])
    assert res.outcome is Outcome.FAIL
    assert res.status is Status.COMPILE_ERROR
    undef = [d for d in res.diagnostics if d.rule_id == "undefined-reference"]
    assert undef and "helper" in undef[0].message
    assert undef[0].tool == "clang"
    assert undef[0].location.file == "src/f.c" and undef[0].location.line == 6
    assert any(d.rule_id == "link-error" for d in res.diagnostics)
    fr = res.failures[0]
    assert fr.kind == "compile" and fr.tool == "clang" and fr.id == "sanitizer:build"
    assert res.details["cases"] == []


def test_second_main_is_compile_error(tmp_path, needs_clang, needs_bwrap):
    src = GOOD_SRC + "int main(void) { return 0; }\n"
    _, res = run_gate(tmp_path, src, [{"name": "ok", "args": ["10"]}])
    assert res.status is Status.COMPILE_ERROR
    assert any(d.rule_id == "multiple-definition" and "main" in d.message for d in res.diagnostics)


def test_syntax_error_is_compile_error(tmp_path, needs_clang, needs_bwrap):
    src = '#include "f.h"\nint f(int n) { return n + ; }\n'
    _, res = run_gate(tmp_path, src, [{"name": "ok", "args": ["10"]}])
    assert res.status is Status.COMPILE_ERROR
    err = next(d for d in res.diagnostics if d.severity == "error")
    assert (err.location.file, err.location.line) == ("src/f.c", 2)


def test_no_sandbox_and_not_allowed_is_skipped(tmp_path, needs_clang):
    _, res = run_gate(tmp_path, GOOD_SRC, [{"name": "ok", "args": ["10"]}], sandbox=Sandbox(kind="none", note="test"))
    assert res.outcome is Outcome.SKIPPED
    assert res.status is Status.UNKNOWN
    assert "refusing" in res.summary and "test" in res.summary
    assert res.details["cases"] == []
    assert [i.tool for i in res.invocations] == ["clang"]


def test_allowed_unsandboxed_runs_with_warning(tmp_path, needs_clang):
    _, res = run_gate(
        tmp_path,
        HBO_SRC,
        [{"name": "oob", "args": ["4"]}],
        sandbox=Sandbox(kind="none", note="test"),
        options=VerifyOptions(allow_unsandboxed=True),
    )
    assert "WITHOUT a sandbox" in res.details["sandbox_warning"]
    assert res.invocations[-1].sandbox == "none"
    assert res.outcome is Outcome.FAIL
    d = next(d for d in res.diagnostics if d.tool == "asan")
    assert (d.location.file, d.location.line) == ("src/f.c", line_of(HBO_SRC, "one past the end"))
    assert "UNSANDBOXED" not in res.summary
    _, ok = run_gate(
        tmp_path / "b", GOOD_SRC, [{"name": "ok", "args": ["10"]}],
        sandbox=Sandbox(kind="none", note="test"), options=VerifyOptions(allow_unsandboxed=True),
    )
    assert ok.outcome is Outcome.PASS and "UNSANDBOXED" in ok.summary
    assert "sandbox_warning" in ok.details


def test_mixed_cases_stdin_and_per_case_dirs(tmp_path, needs_clang, needs_bwrap):
    driver = textwrap.dedent(
        """\
        #include <stdio.h>
        #include <stdlib.h>
        #include <string.h>
        #include "f.h"

        int main(int argc, char **argv)
        {
            char line[64] = {0};
            if (argc > 1 && strcmp(argv[1], "stdin") == 0) {
                if (!fgets(line, sizeof line, stdin)) return 3;
                printf("%d\\n", f(atoi(line)));
                return 0;
            }
            FILE *fp = fopen("scratch.txt", "w"); /* cwd is a private per-case directory */
            if (!fp) return 4;
            fputs("x", fp);
            fclose(fp);
            return f(atoi(argv[1])) == 42 ? 0 : 1;
        }
        """
    )
    cases = [
        {"name": "via_stdin", "args": ["stdin"], "stdin": "10\n", "expect_stdout": "42\n"},
        {"name": "good", "args": ["10"]},
        {"name": "bad", "args": ["11"]},
    ]
    ctx, res = run_gate(tmp_path, GOOD_SRC, cases, driver=driver)
    assert res.outcome is Outcome.FAIL
    status = {c["name"]: c["status"] for c in res.details["cases"]}
    assert status == {"via_stdin": "pass", "good": "pass", "bad": "fail"}
    good = next(c for c in res.details["cases"] if c["name"] == "good")
    assert (ctx.run_dir / good["cwd"] / "scratch.txt").read_text() == "x"
    stdin_fr = [f for f in res.failures if "via_stdin" in f.id]
    assert stdin_fr == []
    assert res.details["counts"]["failed"] == 1 and res.details["counts"]["passed"] == 2


def test_sandboxed_test_cannot_touch_other_run_files(tmp_path, needs_clang, needs_bwrap):
    driver = textwrap.dedent(
        """\
        #include <stdio.h>
        #include <unistd.h>
        #include "f.h"

        int main(int argc, char **argv)
        {
            (void)argc;
            int bad = 0;
            if (access(argv[1], F_OK) == 0) bad |= 1;       /* snapshot dir */
            FILE *fp = fopen(argv[2], "w");                 /* raw dir */
            if (fp) { bad |= 2; fclose(fp); }
            fp = fopen("ok.txt", "w");
            if (!fp) bad |= 4; else fclose(fp);
            printf("%d\\n", bad);
            return f(10) == 42 ? bad : 9;
        }
        """
    )
    root = make_project(tmp_path / "proj", GOOD_SRC, driver)
    run_dir = root / ".leanc" / "runs" / "r1"
    snap = os.path.realpath(run_dir / "snapshot")
    raw_target = os.path.realpath(run_dir / "raw") + "/evil.txt"
    spec = make_spec([{"name": "probe", "args": [snap, raw_target], "expect_stdout": "0\n"}])
    ctx = make_context(spec, root, run_dir, VerifyOptions())
    res = run_sanitizer_tests(ctx)
    assert res.outcome is Outcome.PASS, (res.summary, res.details["cases"])
    assert not os.path.exists(raw_target)


def test_output_flood_is_truncated_and_fails(tmp_path, needs_clang, needs_bwrap):
    driver = textwrap.dedent(
        """\
        #include <stdio.h>
        #include "f.h"
        int main(void)
        {
            for (;;) fputs("spam spam spam spam spam spam spam spam\\n", stdout);
            return f(10) == 42 ? 0 : 1;
        }
        """
    )
    _, res = run_gate(tmp_path, GOOD_SRC, [{"name": "flood"}], driver=driver, max_output_bytes=4096, timeout_s=20)
    assert res.outcome is Outcome.FAIL
    d = next(d for d in res.diagnostics if d.tool == "test-oracle")
    assert d.rule_id == "output-limit-exceeded" and "4096 bytes" in d.message
    case = res.details["cases"][0]
    assert case["output_truncated"] is True and case["duration_s"] < 10


def test_abort_is_reported_with_signal(tmp_path, needs_clang, needs_bwrap):
    src = '#include <stdlib.h>\n#include "f.h"\nint f(int n) { if (n) abort(); return 42; }\n'
    _, res = run_gate(tmp_path, src, [{"name": "boom", "args": ["1"]}])
    assert res.outcome is Outcome.FAIL
    d = next(d for d in res.diagnostics if d.tool == "test-oracle")
    assert d.rule_id == "exit-code" and "SIGABRT" in d.message


def test_sanitizer_control_in_editable_source_fails(tmp_path, needs_clang, needs_bwrap):
    src = textwrap.dedent(
        """\
        #include "f.h"
        /* comments mentioning __asan_poison_memory_region are fine */
        const char *__asan_default_options(void) { return "detect_leaks=0"; }
        int f(int n) { return n + 32; }
        """
    )
    _, res = run_gate(tmp_path, src, [{"name": "ok", "args": ["10"]}])
    assert res.outcome is Outcome.FAIL
    pol = [d for d in res.diagnostics if d.tool == "leanc-policy"]
    assert len(pol) == 1 and pol[0].location.line == 3
    assert any(f.kind == "policy" for f in res.failures)
    assert res.details["cases"][0]["status"] == "pass"


def test_missing_editable_source_is_compile_error(tmp_path, needs_clang):
    root = make_project(tmp_path / "proj", GOOD_SRC)
    (root / "src" / "f.c").unlink()
    ctx = make_context(make_spec([{"name": "ok"}]), root, root / ".leanc" / "runs" / "r1")
    res = run_sanitizer_tests(ctx)
    assert res.outcome is Outcome.FAIL and res.status is Status.COMPILE_ERROR
    assert res.diagnostics[0].rule_id == "missing-source"


def test_missing_clang_is_tool_error(tmp_path):
    root = make_project(tmp_path / "proj", GOOD_SRC)
    ctx = make_context(make_spec([{"name": "ok"}]), root, root / ".leanc" / "runs" / "r1")
    ctx.tools = type(ctx.tools)(clang=None, cbmc=None, clang_version=None, cbmc_version=None)
    res = run_sanitizer_tests(ctx)
    assert res.outcome is Outcome.ERROR and res.status is Status.TOOL_ERROR
    assert "compiler not found" in res.summary


def test_example_safe_array_get_passes(example_copy, needs_clang, needs_bwrap):
    root = example_copy("safe_array_get")
    spec = load_spec(root / "spec.yaml")
    ctx = make_context(spec, root, root / ".leanc" / "runs" / "r1", VerifyOptions())
    res = run_sanitizer_tests(ctx)
    assert res.outcome is Outcome.PASS, (res.summary, res.diagnostics)
    assert [c["status"] for c in res.details["cases"]] == ["pass"] * len(spec.tests.cases)
    assert res.details["sandbox"] == "bwrap"


def test_sanitizer_plan_mapping():
    p = sanitizer_plan(["address", "undefined", "leak"])
    assert p.fsanitize == ("address", "undefined") and p.detect_leaks and p.uses_shadow_runtime
    assert p.compile_flags == ["-fno-sanitize-recover=all", "-fsanitize=address,undefined"]
    p = sanitizer_plan(["leak"])
    assert p.fsanitize == ("leak",) and p.effective == ("leak",) and p.uses_shadow_runtime
    p = sanitizer_plan(["leak", "undefined"])
    assert p.fsanitize == ("address", "undefined") and p.notes
    p = sanitizer_plan(["undefined"])
    assert p.fsanitize == ("undefined",) and not p.detect_leaks and not p.uses_shadow_runtime
    assert sanitizer_plan([]).compile_flags == []


def test_sanitizer_env_options():
    env = sanitizer_env(sanitizer_plan(["address", "undefined", "leak"]), 512, "/usr/bin/llvm-symbolizer")
    asan = env["ASAN_OPTIONS"].split(":")
    for opt in ("detect_leaks=1", "halt_on_error=1", "abort_on_error=0", "exitcode=86", "hard_rss_limit_mb=512",
                "allocator_may_return_null=1", "detect_stack_use_after_return=1", "symbolize=1"):
        assert opt in asan
    assert env["UBSAN_OPTIONS"] == "print_stacktrace=1:halt_on_error=1:exitcode=87"
    assert env["LSAN_OPTIONS"].startswith("exitcode=88")
    assert env["ASAN_SYMBOLIZER_PATH"] == "/usr/bin/llvm-symbolizer"


def test_build_command_order(tmp_path, needs_clang):
    root = make_project(tmp_path / "proj", GOOD_SRC)
    spec = make_spec([{"name": "ok"}])
    spec.compile.defines = ["FOO=1"]
    ctx = make_context(spec, root, root / ".leanc" / "runs" / "r1")
    argv = build_command(ctx, Path("/x/test_bin"), sanitizer_plan(spec.tests.sanitizers))
    assert argv[1:5] == ["-std=c17", "-g", "-O1", "-fno-omit-frame-pointer"]
    i = argv.index
    assert i("-fsanitize=address,undefined") < i("-Wall") < i("-DFOO=1") < i("-Iinclude") < i("src/f.c")
    assert argv[-4:] == ["src/f.c", "tests/driver.c", "-o", "/x/test_bin"]


def test_ubsan_only_build_applies_address_space_limit(tmp_path, needs_clang, needs_bwrap):
    _, res = run_gate(tmp_path, OVF_SRC, [{"name": "ovf", "args": ["100"]}], sanitizers=["undefined"], memory_mb=256)
    assert res.outcome is Outcome.FAIL
    assert [d.tool for d in res.diagnostics] == ["ubsan"]
    assert "-fsanitize=undefined" in res.details["build"]["argv"]


def test_symbolizer_is_under_usr():
    sym = find_llvm_symbolizer("/usr/bin/clang", "Ubuntu clang version 18.1.3")
    if sym is None:
        pytest.skip("llvm-symbolizer not installed")
    assert sym.startswith("/usr/") and os.access(sym, os.X_OK)


SNAP = "/w/run/snapshot"
PF = {"src/f.c", "include/f.h", "tests/driver.c"}

ASAN_HBO = f"""\
=================================================================
==2==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x502000000020 at pc 0x55e5f9129848 bp 0x7fff07db4cb0 sp 0x7fff07db4ca8
READ of size 4 at 0x502000000020 thread T0
    #0 0x55e5f9129847 in f {SNAP}/src/f.c:6:13
    #1 0x55e5f91298ea in main {SNAP}/tests/driver.c:4:43
    #2 0x7ff5f6c2a1c9 in __libc_start_call_main csu/../sysdeps/nptl/libc_start_call_main.h:58:16
    #3 0x7ff5f6c2a28a in __libc_start_main csu/../csu/libc-start.c:360:3
    #4 0x55e5f904f344 in _start (/w/run/build/sanitizer/test_bin+0x2c344) (BuildId: 61ee8e4fc3a458edb925d212ce4d8be7cd8e77d3)

0x502000000020 is located 0 bytes after 16-byte region [0x502000000010,0x502000000020)
allocated by thread T0 here:
    #0 0x55e5f90e9193 in malloc (/w/run/build/sanitizer/test_bin+0xc7193) (BuildId: 61ee8e4fc3a458edb925d212ce4d8be7cd8e77d3)
    #1 0x55e5f91297cc in f {SNAP}/src/f.c:3:14

SUMMARY: AddressSanitizer: heap-buffer-overflow {SNAP}/src/f.c:6:13 in f
Shadow bytes around the buggy address:
  0x501ffffffd80: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
=>0x502000000000: fa fa 00 00[fa]fa fa fa fa fa fa fa fa fa fa fa
==2==ABORTING
"""

ASAN_DOUBLE_FREE = f"""\
==2==ERROR: AddressSanitizer: attempting double-free on 0x502000000010 in thread T0:
    #0 0x5610c7291efa in free (/w/run/build/sanitizer/test_bin+0xc6efa) (BuildId: 61ee8e4fc3a458edb925d212ce4d8be7cd8e77d3)
    #1 0x5610c72d089e in f {SNAP}/src/f.c:11:28
    #2 0x5610c72d0a3a in main {SNAP}/tests/driver.c:4:43

0x502000000010 is located 0 bytes inside of 16-byte region [0x502000000010,0x502000000020)
freed by thread T0 here:
    #0 0x5610c7291efa in free (/w/run/build/sanitizer/test_bin+0xc6efa) (BuildId: 61ee8e4fc3a458edb925d212ce4d8be7cd8e77d3)
    #1 0x5610c72d0896 in f {SNAP}/src/f.c:11:19

SUMMARY: AddressSanitizer: double-free (/w/run/build/sanitizer/test_bin+0xc6efa) (BuildId: 61ee8e4fc3a458edb925d212ce4d8be7cd8e77d3) in free
==2==ABORTING
"""

ASAN_SEGV = f"""\
AddressSanitizer:DEADLYSIGNAL
=================================================================
==2==ERROR: AddressSanitizer: SEGV on unknown address 0x000000000000 (pc 0x55aec9aaa8b3 bp 0x7ffc6cde9140 sp 0x7ffc6cde90c0 T0)
==2==The signal is caused by a READ memory access.
==2==Hint: address points to the zero page.
    #0 0x55aec9aaa8b3 in f {SNAP}/src/f.c:12:57
    #1 0x55aec9aaaa3a in main {SNAP}/tests/driver.c:4:43

AddressSanitizer can not provide additional info.
SUMMARY: AddressSanitizer: SEGV {SNAP}/src/f.c:12:57 in f
==2==ABORTING
"""

ASAN_STACK = f"""\
==7==ERROR: AddressSanitizer: stack-buffer-overflow on address 0x7ffd1c2f0a48 at pc 0x55 bp 0x7ffd sp 0x7ffd
WRITE of size 4 at 0x7ffd1c2f0a48 thread T0
    #0 0x55d1 in fill {SNAP}/src/f.c:5:14
    #1 0x55d2 in f {SNAP}/src/f.c:10:5
    #2 0x55d3 in main {SNAP}/tests/driver.c:4:43

Address 0x7ffd1c2f0a48 is located in stack of thread T0 at offset 40 in frame
    #0 0x55d4 in f {SNAP}/src/f.c:8

  This frame has 1 object(s):
    [32, 40) 'buf' (line 9) <== Memory access at offset 40 overflows this variable
SUMMARY: AddressSanitizer: stack-buffer-overflow {SNAP}/src/f.c:5:14 in fill
"""

UBSAN_OVF = f"""\
src/f.c:6:16: runtime error: signed integer overflow: 2147483600 + 100 cannot be represented in type 'int'
    #0 0x55fe5604c981 in f {SNAP}/src/f.c:6:16
    #1 0x55fe5604ca3a in main {SNAP}/tests/driver.c:8:13
    #2 0x7fd45022a1c9 in __libc_start_call_main csu/../sysdeps/nptl/libc_start_call_main.h:58:16

SUMMARY: UndefinedBehaviorSanitizer: undefined-behavior src/f.c:6:16
"""

LSAN_COMBINED = f"""\
r=42

=================================================================
==2==ERROR: LeakSanitizer: detected memory leaks

Direct leak of 64 byte(s) in 1 object(s) allocated from:
    #0 0x55780ee72193 in malloc (/w/run/build/sanitizer/test_bin+0xc7193) (BuildId: 61ee8e4fc3a458edb925d212ce4d8be7cd8e77d3)
    #1 0x55780eeb07cc in f {SNAP}/src/f.c:9:17
    #2 0x55780eeb0a3a in main {SNAP}/tests/driver.c:8:13
    #3 0x7f38f822a1c9 in __libc_start_call_main csu/../sysdeps/nptl/libc_start_call_main.h:58:16

Indirect leak of 8 byte(s) in 2 object(s) allocated from:
    #0 0x55780ee72193 in malloc (/w/run/build/sanitizer/test_bin+0xc7193) (BuildId: 61ee8e4fc3a458edb925d212ce4d8be7cd8e77d3)
    #1 0x55780eeb07dd in make_node {SNAP}/src/f.c:20:9

SUMMARY: AddressSanitizer: 72 byte(s) leaked in 3 allocation(s).
"""


def test_parse_asan_heap_buffer_overflow():
    diags = parse_sanitizer_output(ASAN_HBO, project_files=PF, base_dir=SNAP)
    assert len(diags) == 1
    d = diags[0]
    assert (d.tool, d.rule_id, d.severity) == ("asan", "heap-buffer-overflow", "error")
    assert (d.location.file, d.location.line, d.location.column, d.location.function) == ("src/f.c", 6, 13, "f")
    assert "READ of size 4" in d.message and "0 bytes after 16-byte region" in d.message
    assert "0x502000000020" not in d.message
    assert d.raw.startswith("==2==ERROR: AddressSanitizer: heap-buffer-overflow")
    assert "Shadow bytes" not in d.raw and "SUMMARY" in d.raw


def test_parse_location_without_base_dir_uses_suffix_match():
    d = parse_sanitizer_output(ASAN_HBO, project_files=PF)[0]
    assert (d.location.file, d.location.line) == ("src/f.c", 6)


def test_parse_location_without_project_files_uses_first_non_system_frame():
    d = parse_sanitizer_output(ASAN_DOUBLE_FREE)[0]
    assert d.location.file == f"{SNAP}/src/f.c" and d.location.line == 11
    d = parse_sanitizer_output(ASAN_DOUBLE_FREE, base_dir=SNAP)[0]
    assert d.location.file == "src/f.c"


def test_parse_location_prefers_project_frame_over_other_files():
    text = ASAN_HBO.replace(f"in f {SNAP}/src/f.c:6:13", "in helper /home/x/other/lib.c:3:1")
    d = parse_sanitizer_output(text, project_files=PF, base_dir=SNAP)[0]
    assert d.location.file == "tests/driver.c"


def test_parse_double_free_and_segv_and_stack_overflow():
    d = parse_sanitizer_output(ASAN_DOUBLE_FREE, project_files=PF, base_dir=SNAP)[0]
    assert d.rule_id == "double-free" and (d.location.file, d.location.line) == ("src/f.c", 11)
    d = parse_sanitizer_output(ASAN_SEGV, project_files=PF, base_dir=SNAP)[0]
    assert d.rule_id == "SEGV" and (d.location.file, d.location.line) == ("src/f.c", 12)
    assert "zero page" in d.message and "READ memory access" in d.message
    d = parse_sanitizer_output(ASAN_STACK, project_files=PF, base_dir=SNAP)[0]
    assert d.rule_id == "stack-buffer-overflow"
    assert (d.location.file, d.location.line, d.location.function) == ("src/f.c", 5, "fill")
    assert "WRITE of size 4" in d.message and "'buf'" in d.message


def test_parse_header_kind_without_summary():
    text = "==3==ERROR: AddressSanitizer: heap-use-after-free on address 0x6 at pc 0x1 bp 0x2 sp 0x3\n" \
           f"READ of size 4 at 0x6 thread T0\n    #0 0x1 in f {SNAP}/src/f.c:7:32\n"
    d = parse_sanitizer_output(text, project_files=PF, base_dir=SNAP)[0]
    assert d.rule_id == "heap-use-after-free" and d.location.line == 7
    bad_free = "==3==ERROR: AddressSanitizer: attempting free on address which was not malloc()-ed: 0x7ffd in thread T0\n"
    assert parse_sanitizer_output(bad_free)[0].rule_id == "bad-free"


def test_parse_ubsan_signed_overflow():
    findings = parse_sanitizer_findings(UBSAN_OVF, project_files=PF, base_dir=SNAP)
    assert len(findings) == 1
    d = findings[0].diagnostic
    assert (d.tool, d.rule_id) == ("ubsan", "signed-integer-overflow")
    assert (d.location.file, d.location.line, d.location.column, d.location.function) == ("src/f.c", 6, 16, "f")
    assert findings[0].call_stack()[0] == "#0 f src/f.c:6:16"
    assert "SUMMARY" in d.raw


@pytest.mark.parametrize(
    "msg, rule",
    [
        ("signed integer overflow: 2147483647 + 1 cannot be represented in type 'int'", "signed-integer-overflow"),
        ("negation of -2147483648 cannot be represented in type 'int'", "signed-integer-overflow"),
        ("division of -2147483648 by -1 cannot be represented in type 'int'", "signed-integer-overflow"),
        ("division by zero", "division-by-zero"),
        ("index 4 out of bounds for type 'int[4]'", "out-of-bounds-index"),
        ("load of misaligned address 0x7ff77fc00041 for type 'int', which requires 4 byte alignment", "misaligned"),
        ("load of null pointer of type 'int'", "null-pointer"),
        ("null pointer passed as argument 1, which is declared to never be null", "null-pointer"),
        ("applying zero offset to null pointer", "pointer-overflow"),
        ("shift exponent 33 is too large for 32-bit type 'int'", "shift"),
        ("left shift of negative value -1", "shift"),
        ("load of address 0x5 with insufficient space for an object of type 'int'", "object-size"),
        ("load of value 7, which is not a valid value for type '_Bool'", "invalid-value"),
        ("something new and unexpected", "ubsan-runtime-error"),
    ],
)
def test_ubsan_rule_mapping(msg, rule):
    assert ubsan_rule(msg) == rule
    d = parse_sanitizer_output(f"src/f.c:3:9: runtime error: {msg}\n", project_files=PF)[0]
    assert d.rule_id == rule and d.location.line == 3


def test_parse_lsan_direct_and_indirect_leaks():
    diags = parse_sanitizer_output(LSAN_COMBINED, project_files=PF, base_dir=SNAP)
    assert [d.tool for d in diags] == ["lsan", "lsan"]
    assert all(d.rule_id == "memory-leak" for d in diags)
    direct, indirect = diags
    assert "Direct leak of 64 byte(s) in 1 object(s)" in direct.message
    assert (direct.location.file, direct.location.line, direct.location.function) == ("src/f.c", 9, "f")
    assert "Indirect leak of 8 byte(s)" in indirect.message and indirect.location.function == "make_node"


def test_parse_standalone_lsan_and_rss_limit():
    text = (
        "==2==ERROR: LeakSanitizer: detected memory leaks\n\n"
        "Direct leak of 16 byte(s) in 1 object(s) allocated from:\n"
        "    #0 0x5578 in malloc (/w/run/build/sanitizer/test_bin+0x33239) (BuildId: edf0)\n"
        f"    #1 0x5578 in f {SNAP}/src/f.c:4:14\n\n"
        "SUMMARY: LeakSanitizer: 16 byte(s) leaked in 1 allocation(s).\n"
    )
    d = parse_sanitizer_output(text, project_files=PF, base_dir=SNAP)
    assert len(d) == 1 and d[0].tool == "lsan" and d[0].location.line == 4
    rss = "==9==AddressSanitizer: hard rss limit exhausted (300Mb vs 315Mb)\n==9==Process memory map follows:\n\t0x1-0x2\t/usr/lib/x\n"
    d = parse_sanitizer_output(rss)
    assert len(d) == 1 and d[0].tool == "asan" and d[0].rule_id == "rss-limit-exceeded"
    assert "300Mb vs 315Mb" in d[0].message and "memory map" not in d[0].raw


def test_parse_summary_only_and_clean_output():
    assert parse_sanitizer_output("r=42\nall good\n") == []
    assert parse_sanitizer_output("") == []
    d = parse_sanitizer_output("SUMMARY: UndefinedBehaviorSanitizer: undefined-behavior src/f.c:8:42\n",
                               project_files=PF)
    assert len(d) == 1 and d[0].tool == "ubsan" and d[0].rule_id == "ubsan-runtime-error"
    assert (d[0].location.file, d[0].location.line) == ("src/f.c", 8)


def test_parse_multiple_reports_in_one_stream():
    text = UBSAN_OVF + "src/f.c:9:3: runtime error: division by zero\n" + ASAN_HBO
    diags = parse_sanitizer_output(text, project_files=PF, base_dir=SNAP)
    assert [(d.tool, d.rule_id) for d in diags] == [
        ("ubsan", "signed-integer-overflow"),
        ("ubsan", "division-by-zero"),
        ("asan", "heap-buffer-overflow"),
    ]


def test_parse_build_diagnostics_linker_and_compiler():
    text = (
        "src/f.c:3:12: warning: unused variable 'x' [-Wunused-variable]\n"
        "/usr/bin/ld: /tmp/f-26a909.o: in function `f':\n"
        f"{SNAP}/src/f.c:6:(.text+0xe6): undefined reference to `helper'\n"
        "clang: error: linker command failed with exit code 1 (use -v to see invocation)\n"
    )
    diags = parse_build_diagnostics(text, base_dir=SNAP)
    assert [d.rule_id for d in diags] == ["-Wunused-variable", "undefined-reference", "link-error"]
    ref = diags[1]
    assert (ref.location.file, ref.location.line, ref.location.function) == ("src/f.c", 6, "f")
    assert ref.message == "undefined reference to `helper'"


def test_scan_sanitizer_control(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.c").write_text(
        textwrap.dedent(
            """\
            // __lsan_is_turned_off in a comment is fine
            const char *s = "/* not a comment";
            int __lsan_is_turned_off(void) { return 1; }
            __attribute__((no_sanitize("address"))) int g(int *p) { return *p; }
            #include <sanitizer/asan_interface.h>
            #if __has_feature(address_sanitizer)
            #endif
            /* __asan_default_options */
            """
        )
    )
    diags = scan_sanitizer_control(tmp_path, ["src/a.c"])
    assert sorted(d.location.line for d in diags) == [3, 4, 5, 6]
    assert all(d.tool == "leanc-policy" and d.rule_id == "sanitizer-control" for d in diags)


def test_parse_frame_without_line_info():
    text = (
        "src/sensor.c:14:13: runtime error: member access within null pointer of type 'const struct sensor'\n"
        f"    #0 0x562511d41880 in sensor_read {SNAP}/src/sensor.c\n"
        f"    #1 0x562511d41c04 in main {SNAP}/tests/driver.c:42:16\n"
        "SUMMARY: UndefinedBehaviorSanitizer: undefined-behavior src/sensor.c:14:13\n"
    )
    f = parse_sanitizer_findings(text, project_files={"src/sensor.c", "tests/driver.c"}, base_dir=SNAP)[0]
    d = f.diagnostic
    assert (d.rule_id, d.location.file, d.location.line, d.location.function) == (
        "null-pointer", "src/sensor.c", 14, "sensor_read"
    )
    assert f.call_stack()[:2] == ["#0 sensor_read src/sensor.c", "#1 main tests/driver.c:42:16"]
    asan = "==1==ERROR: AddressSanitizer: SEGV on unknown address 0x000000000008\n" \
           f"    #0 0x1 in sensor_read {SNAP}/src/sensor.c\n"
    d = parse_sanitizer_output(asan, project_files={"src/sensor.c"}, base_dir=SNAP)[0]
    assert (d.location.file, d.location.line, d.location.function) == ("src/sensor.c", None, "sensor_read")


def test_parse_ubsan_without_location_and_no_false_positive():
    d = parse_sanitizer_output("<unknown>: runtime error: division by zero\n")
    assert len(d) == 1 and d[0].rule_id == "division-by-zero" and d[0].location is None
    d = parse_sanitizer_output("(/w/bin/test_bin+0x1234): runtime error: load of null pointer of type 'int'\n")
    assert len(d) == 1 and d[0].rule_id == "null-pointer" and d[0].location is None
    assert parse_sanitizer_output("mytool: runtime error: config file missing\n") == []
