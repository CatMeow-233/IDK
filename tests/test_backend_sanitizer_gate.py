from __future__ import annotations

import os
import textwrap
import time
from pathlib import Path

from sanitizer_support import DRIVER, GOOD_SRC, make_project, make_spec

from leanc.backends.sanitizer import LIMITATION, run_sanitizer_tests
from leanc.foundation.sandbox import Sandbox
from leanc.foundation.status import Outcome, Status
from leanc.specs.loader import load_spec
from leanc.toolchain.context import VerifyOptions, make_context


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


def test_ubsan_only_build_applies_address_space_limit(tmp_path, needs_clang, needs_bwrap):
    _, res = run_gate(tmp_path, OVF_SRC, [{"name": "ovf", "args": ["100"]}], sanitizers=["undefined"], memory_mb=256)
    assert res.outcome is Outcome.FAIL
    assert [d.tool for d in res.diagnostics] == ["ubsan"]
    assert "-fsanitize=undefined" in res.details["build"]["argv"]
