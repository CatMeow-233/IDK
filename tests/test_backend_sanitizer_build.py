from __future__ import annotations

import os
from pathlib import Path

import pytest
from sanitizer_support import GOOD_SRC, make_project, make_spec

from leanc.backends.sanitizer import (
    build_command,
    find_llvm_symbolizer,
    sanitizer_env,
    sanitizer_plan,
)
from leanc.toolchain.context import make_context


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


def test_symbolizer_is_under_usr():
    sym = find_llvm_symbolizer("/usr/bin/clang", "Ubuntu clang version 18.1.3")
    if sym is None:
        pytest.skip("llvm-symbolizer not installed")
    assert sym.startswith("/usr/") and os.access(sym, os.X_OK)
