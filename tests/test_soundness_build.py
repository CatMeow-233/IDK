from __future__ import annotations

import textwrap
from pathlib import Path

from sanitizer_support import make_project, make_spec

from leanc.backends.lean.vectors import VectorCheck
from leanc.backends.sanitizer import build, run_sanitizer_tests, sanitizer_plan
from leanc.backends.sanitizer.build import (
    BASE_FLAGS,
    BUILD_FIDELITY_FLAGS,
    build_command,
)
from leanc.backends.sanitizer.ownership_build import build_steps
from leanc.foundation.status import Outcome
from leanc.specs.model import LeanVectors
from leanc.toolchain.context import make_context
from leanc.toolchain.cxx import compiler

EXPECTED_FIDELITY_FLAGS = (
    "-fno-builtin-malloc",
    "-fno-builtin-calloc",
    "-fno-builtin-realloc",
    "-fno-builtin-free",
    "-fno-optimize-sibling-calls",
)
SIBLING_CALL_FLAG = "-fno-optimize-sibling-calls"
SPEC_SIBLING_CALLS_FLAG = "-foptimize-sibling-calls"
SPEC_FLAGS = ["-Wall", SPEC_SIBLING_CALLS_FLAG]
SANITIZE_FLAG = "-fsanitize=address,undefined"
VECTOR_DRIVER = "tests/vector_driver.c"
TRACE_BUILD_TOOL = "ownership-trace-build"
LEAK_CASE = {"name": "leaky", "args": ["10"], "expect_stdout": "r=42\n"}
FIRST_ALLOCATION = "first allocation"
WRAPPED_ALLOCATION = "wrapped allocation"

OVERWRITTEN_ALLOCATION_SRC = textwrap.dedent(
    """\
    #include <stdlib.h>
    #include "f.h"

    int f(int n)
    {
        int *a = malloc(4 * sizeof(int)); /* first allocation */
        a = malloc(4 * sizeof(int));
        if (!a) return -1;
        a[0] = n + 32;
        int r = a[0];
        free(a);
        return r;
    }
    """
)

RELEASED_ALLOCATIONS_SRC = textwrap.dedent(
    """\
    #include <stdlib.h>
    #include "f.h"

    int f(int n)
    {
        int *a = malloc(4 * sizeof(int));
        free(a);
        a = malloc(4 * sizeof(int));
        if (!a) return -1;
        a[0] = n + 32;
        int r = a[0];
        free(a);
        return r;
    }
    """
)

WRAPPER_LEAK_SRC = textwrap.dedent(
    """\
    #include <stdlib.h>
    #include "f.h"

    void *make_buf(size_t n);

    __attribute__((noinline)) void *make_buf(size_t n)
    {
        return malloc(n); /* wrapped allocation */
    }

    int f(int n)
    {
        char *buf = make_buf(64);
        if (!buf) return -1;
        buf[0] = (char)n;
        volatile char sink = buf[0];
        (void)sink;
        return n + 32;
    }
    """
)


def _context(tmp_path: Path, src: str = RELEASED_ALLOCATIONS_SRC, flags: list[str] | None = None):
    root = make_project(tmp_path / "proj", src)
    spec = make_spec([LEAK_CASE])
    if flags is not None:
        spec.compile.flags = flags
    return make_context(spec, root, root / ".leanc" / "runs" / "r1")


def _run_gate(tmp_path: Path, src: str):
    return run_sanitizer_tests(_context(tmp_path, src))


def _line_of(src: str, needle: str) -> int:
    return next(number for number, line in enumerate(src.splitlines(), 1) if needle in line)


def _allocation_site(failure) -> tuple[str | None, int | None]:
    assert failure.ownership_trace is not None
    allocation = failure.ownership_trace.allocation
    return allocation.file, allocation.line


def _fidelity_start(argv: list[str]) -> int:
    start = argv.index(BUILD_FIDELITY_FLAGS[0])
    assert tuple(argv[start : start + len(BUILD_FIDELITY_FLAGS)]) == BUILD_FIDELITY_FLAGS
    return start


def _assert_fidelity_after_spec_flags(argv: list[str]) -> None:
    start = _fidelity_start(argv)
    assert argv.index(SPEC_SIBLING_CALLS_FLAG) < start < argv.index(SANITIZE_FLAG)


def _without_fidelity(argv: list[str]) -> list[str]:
    return [arg for arg in argv if arg not in BUILD_FIDELITY_FLAGS]


def _trace_steps(ctx, tmp_path: Path) -> dict[str, list[str]]:
    steps, _ = build_steps(ctx, sanitizer_plan(ctx.spec.tests.sanitizers), tmp_path / "trace")
    return dict(steps)


def _vector_argv(ctx, tmp_path: Path) -> list[str]:
    check = VectorCheck(
        ctx, None, None, None, LeanVectors(generator="spec/Gen.lean", driver=VECTOR_DRIVER),
        (tmp_path, tmp_path, tmp_path), [], [],
    )
    check.cc = compiler(ctx)
    check.plan = sanitizer_plan(ctx.spec.tests.sanitizers)
    return check._build_argv()


def test_fidelity_flags_are_the_required_tuple():
    assert BUILD_FIDELITY_FLAGS == EXPECTED_FIDELITY_FLAGS


def test_sanitizer_build_puts_fidelity_flags_after_spec_flags(tmp_path, needs_clang):
    ctx = _context(tmp_path, flags=SPEC_FLAGS)
    argv = build_command(ctx, Path("/x/test_bin"), sanitizer_plan(ctx.spec.tests.sanitizers))
    _assert_fidelity_after_spec_flags(argv)
    assert _without_fidelity(argv)[1:7] == ["-std=c17", *BASE_FLAGS, *SPEC_FLAGS]


def test_trace_build_compiles_project_sources_with_fidelity_flags(tmp_path, needs_clang):
    steps = _trace_steps(_context(tmp_path, flags=SPEC_FLAGS), tmp_path)
    for label in ("compile src/f.c", "compile tests/driver.c"):
        _assert_fidelity_after_spec_flags(steps[label])
    for label in ("compile runtime", "link"):
        assert not set(BUILD_FIDELITY_FLAGS) & set(steps[label])


def test_vector_driver_build_shares_the_sanitizer_build_argv(tmp_path, needs_clang):
    ctx = _context(tmp_path, flags=SPEC_FLAGS)
    vector_argv = _vector_argv(ctx, tmp_path)
    _assert_fidelity_after_spec_flags(vector_argv)
    binary = Path(vector_argv[-1])
    test_argv = build_command(ctx, binary, sanitizer_plan(ctx.spec.tests.sanitizers))
    assert vector_argv == [VECTOR_DRIVER if arg == ctx.spec.tests.driver else arg for arg in test_argv]


def test_one_constant_governs_every_instrumented_build(tmp_path, needs_clang, monkeypatch):
    ctx = _context(tmp_path, flags=SPEC_FLAGS)
    monkeypatch.setattr(build, "BUILD_FIDELITY_FLAGS", ())
    steps = _trace_steps(ctx, tmp_path)
    argvs = [
        build_command(ctx, Path("/x/test_bin"), sanitizer_plan(ctx.spec.tests.sanitizers)),
        steps["compile src/f.c"],
        steps["compile tests/driver.c"],
        _vector_argv(ctx, tmp_path),
    ]
    for argv in argvs:
        assert not set(EXPECTED_FIDELITY_FLAGS) & set(argv)


def test_overwritten_unused_allocation_is_reported_as_leak(tmp_path, needs_clang, needs_bwrap):
    res = _run_gate(tmp_path, OVERWRITTEN_ALLOCATION_SRC)
    assert res.outcome is Outcome.FAIL, res.summary
    leaks = [d for d in res.diagnostics if d.tool == "lsan"]
    assert [d.rule_id for d in leaks] == ["memory-leak"], res.diagnostics
    location = leaks[0].location
    assert (location.file, location.line) == ("src/f.c", _line_of(OVERWRITTEN_ALLOCATION_SRC, FIRST_ALLOCATION))
    failure = next(f for f in res.failures if f.tool == "lsan")
    assert failure.kind == "runtime"
    assert _allocation_site(failure) == (location.file, location.line)
    trace_compiles = [i.argv for i in res.invocations if i.tool == TRACE_BUILD_TOOL and "src/f.c" in i.argv]
    assert trace_compiles
    for argv in trace_compiles:
        assert _fidelity_start(argv) > 0


def test_overwritten_allocation_escapes_lsan_without_fidelity_flags(tmp_path, needs_clang, needs_bwrap, monkeypatch):
    monkeypatch.setattr(build, "BUILD_FIDELITY_FLAGS", ())
    res = _run_gate(tmp_path, OVERWRITTEN_ALLOCATION_SRC)
    assert res.outcome is Outcome.PASS, res.summary


def test_released_allocations_still_pass(tmp_path, needs_clang, needs_bwrap):
    res = _run_gate(tmp_path, RELEASED_ALLOCATIONS_SRC)
    assert res.outcome is Outcome.PASS, res.summary
    assert res.failures == [] and res.diagnostics == []
    assert _fidelity_start(res.details["build"]["argv"]) > 0


def test_wrapper_frame_locates_the_leak_inside_the_wrapper(tmp_path, needs_clang, needs_bwrap):
    res = _run_gate(tmp_path, WRAPPER_LEAK_SRC)
    assert res.outcome is Outcome.FAIL, res.summary
    leak = next(d for d in res.diagnostics if d.tool == "lsan")
    assert (leak.location.file, leak.location.line) == ("src/f.c", _line_of(WRAPPER_LEAK_SRC, WRAPPED_ALLOCATION))
    assert leak.location.function == "make_buf"
    failure = next(f for f in res.failures if f.tool == "lsan")
    assert _allocation_site(failure) == (leak.location.file, leak.location.line)


def test_sibling_calls_hide_the_wrapper_frame_without_the_flag(tmp_path, needs_clang, needs_bwrap, monkeypatch):
    without_sibling_flag = tuple(flag for flag in EXPECTED_FIDELITY_FLAGS if flag != SIBLING_CALL_FLAG)
    monkeypatch.setattr(build, "BUILD_FIDELITY_FLAGS", without_sibling_flag)
    res = _run_gate(tmp_path, WRAPPER_LEAK_SRC)
    assert res.outcome is Outcome.FAIL, res.summary
    leak = next(d for d in res.diagnostics if d.tool == "lsan")
    assert leak.location.line != _line_of(WRAPPER_LEAK_SRC, WRAPPED_ALLOCATION)

