"""Tests for the clang compile gate (leanc.backends.clang)."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from leanc.backends.clang import compile_command, parse_clang_diagnostics, run_compile
from leanc.foundation.status import Outcome, Status
from leanc.specs.loader import load_spec
from leanc.specs.model import Spec
from leanc.toolchain.context import BackendContext, make_context
from leanc.toolchain.environment import Toolchain

# --------------------------------------------------------------------------- helpers

HEADER = """#ifndef A_H
#define A_H
int f(int x);
#endif
"""

CLEAN = """#include "a.h"
int f(int x)
{
    return x + 1;
}
"""

SYNTAX_ERROR = """#include "a.h"
int f(int x)
{
    int y = x
    return y;
}
"""

WARNING = """#include "a.h"
int f(int x)
{
    int unused;
    return x;
}
"""


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def make_project(tmp_path: Path, files: dict[str, str], editable: list[str] | None = None, **over) -> BackendContext:
    root = tmp_path / "proj"
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    root.mkdir(parents=True, exist_ok=True)
    editable = editable or [r for r in files if r.startswith("src/") and r.endswith(".c")]
    headers = [r for r in files if r.startswith("include/")]
    data = {
        "spec_id": "unit-test",
        "version": 1,
        "title": "unit test",
        "requirement": "test",
        "target": {"function": "f", "signature": "int f(int x)", "returns": {"type": "int"}},
        "sources": {"editable": editable, "headers": headers, "include_dirs": ["include"]},
        "verification": {"harness": "harness/h.c"},
        "gates": ["compile", "static_analysis"],
    }
    spec = Spec.model_validate(_merge(data, over))
    return make_context(spec, root, tmp_path / "run")


def fake_tool(tmp_path: Path, name: str, body: str) -> str:
    d = tmp_path / "fakebin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    p.chmod(0o755)
    return str(p)


# --------------------------------------------------------------------------- parser


def test_parse_warning_with_flag():
    [d] = parse_clang_diagnostics("src/a.c:12:5: warning: unused variable 'x' [-Wunused-variable]\n")
    assert d.tool == "clang"
    assert d.severity == "warning"
    assert d.rule_id == "-Wunused-variable"
    assert d.message == "unused variable 'x'"
    assert (d.location.file, d.location.line, d.location.column) == ("src/a.c", 12, 5)
    assert d.raw == "src/a.c:12:5: warning: unused variable 'x' [-Wunused-variable]"


def test_parse_error_kinds_and_werror():
    text = "\n".join(
        [
            "src/a.c:2:14: error: expected ';' at end of declaration",
            "src/a.c:1:10: fatal error: 'nosuch.h' file not found",
            "src/w.c:2:9: error: unused variable 'unused' [-Werror,-Wunused-variable]",
            "src/w.c:3:1: warning: something odd",
            "fatal error: too many errors emitted, stopping now [-ferror-limit=]",
        ]
    )
    ds = parse_clang_diagnostics(text, tool="clang")
    assert [(d.severity, d.rule_id) for d in ds] == [
        ("error", "clang-error"),
        ("error", "clang-fatal"),
        ("error", "-Wunused-variable"),
        ("warning", "clang-warning"),
        ("error", "clang-fatal"),
    ]
    assert ds[2].message == "unused variable 'unused'"
    assert ds[4].message == "too many errors emitted, stopping now"
    assert ds[4].location is None


def test_parse_notes_attach_to_preceding_diagnostic():
    text = "\n".join(
        [
            "note: orphan note before anything",
            "src/a.c:5:16: warning: Dereference of null pointer [core.NullDereference]",
            "src/a.c:3:5: note: 'q' initialized to a null pointer value",
            "src/a.c:4:9: note: Assuming 'p' is equal to NULL",
            "src/b.c:1:1: error: redefinition of 'f'",
            "src/a.c:1:5: note: previous definition is here",
        ]
    )
    ds = parse_clang_diagnostics(text, tool="clang-static-analyzer")
    assert [d.severity for d in ds] == ["note", "warning", "error"]  # notes are not double-counted
    orphan, w, e = ds
    assert orphan.message == "orphan note before anything" and orphan.rule_id is None
    assert w.rule_id == "core.NullDereference" and w.tool == "clang-static-analyzer"
    assert [s.message for s in w.path] == ["'q' initialized to a null pointer value", "Assuming 'p' is equal to NULL"]
    assert w.path[0].location.line == 3 and w.path[1].location.column == 9
    assert "initialized to a null pointer" in w.raw  # note text kept in raw too
    assert e.path[0].location.file == "src/a.c" and e.path[0].message == "previous definition is here"


def test_parse_locations_without_line_and_driver_messages():
    text = "\n".join(
        [
            "clang: error: no such file or directory: 'nosuch.c'",
            "clang-18: warning: argument unused during compilation: '-pg' [-Wunused-command-line-argument]",
            "/usr/lib/llvm-18/bin/clang: error: unable to open output file",
            "src/b.c: error: cannot read file",
            "error: no analyzer checkers or packages are associated with 'nosuch.Checker'",
            "note: use -analyzer-disable-all-checks to disable all static analyzer checkers",
        ]
    )
    ds = parse_clang_diagnostics(text)
    assert len(ds) == 5
    assert ds[0].location is None and ds[0].rule_id == "clang-error"
    assert ds[1].location is None and ds[1].rule_id == "-Wunused-command-line-argument"
    assert ds[2].location is None
    assert ds[3].location.file == "src/b.c" and ds[3].location.line is None
    assert ds[4].location is None and ds[4].path[0].message.startswith("use -analyzer-disable")


def test_parse_gnu_ld_linker_errors(tmp_path):
    base = tmp_path / "snap"
    base.mkdir()
    text = "\n".join(
        [
            "/usr/bin/ld: /tmp/main-b0a5a9.o: in function `main':",
            "main.c:(.text+0x10): undefined reference to `foo'",
            "clang: error: linker command failed with exit code 1 (use -v to see invocation)",
            "/usr/bin/ld: /tmp/main-e25ef3.o: in function `helper':",
            f"{base}/src/main.c:2:(.text+0x10): undefined reference to `bar'",
            "/usr/bin/ld: /tmp/x.o:(.data+0x0): multiple definition of `g'; /tmp/y.o:(.data+0x0): first defined here",
            "/usr/bin/ld: cannot find -lnosuchlib: No such file or directory",
            "/usr/bin/ld: warning: x.o: missing .note.GNU-stack section implies executable stack",
            "collect2: error: ld returned 1 exit status",
        ]
    )
    ds = parse_clang_diagnostics(text, base_dir=base)
    assert [d.rule_id for d in ds] == ["linker-error"] * 5 + ["linker-warning", "linker-error"]
    undef_foo, cmd_failed, undef_bar, multi, cannot, warn, collect2 = ds
    assert undef_foo.message == "undefined reference to `foo'" and undef_foo.severity == "error"
    assert undef_foo.location.file == "main.c" and undef_foo.location.function == "main"
    assert "in function `main'" in undef_foo.raw
    assert cmd_failed.message.startswith("linker command failed") and cmd_failed.location is None
    assert undef_bar.location.file == "src/main.c" and undef_bar.location.line == 2  # relativised
    assert undef_bar.location.function == "helper"
    assert multi.message.startswith("multiple definition of `g'")
    assert multi.location is None  # an object file is not a source location
    assert cannot.message.startswith("cannot find -lnosuchlib")
    assert warn.severity == "warning"
    assert collect2.message == "ld returned 1 exit status" and collect2.severity == "error"


def test_parse_lld_errors():
    text = "\n".join(
        [
            "ld.lld: error: undefined symbol: foo",
            ">>> referenced by main.c",
            ">>>               /tmp/main-4df3bc.o:(main)",
            "clang: error: linker command failed with exit code 1 (use -v to see invocation)",
        ]
    )
    ds = parse_clang_diagnostics(text)
    assert [d.rule_id for d in ds] == ["linker-error", "linker-error"]
    assert ds[0].message == "undefined symbol: foo"
    assert [s.message for s in ds[0].path] == ["referenced by main.c", "/tmp/main-4df3bc.o:(main)"]


def test_parse_ignores_summaries_keeps_context_in_raw():
    text = "\n".join(
        [
            "In file included from src/a.c:1:",
            "include/a.h:3:5: warning: unused variable 'x' [-Wunused-variable]",
            "    3 |     int x;",
            "      |         ^",
            "src/a.c:2:1: remark: foo inlined into bar [-Rpass=inline]",
            "src/a.c:9:1: error: odd message [not an option]",
            "1 warning and 1 error generated.",
            "some unrelated line",
            "",
        ]
    )
    ds = parse_clang_diagnostics(text)
    assert len(ds) == 3
    w, r, e = ds
    assert w.location.file == "include/a.h"
    assert w.raw.splitlines()[0] == "In file included from src/a.c:1:"
    assert "    3 |     int x;" in w.raw
    assert r.severity == "info" and r.rule_id == "-Rpass=inline"
    assert e.message == "odd message [not an option]" and e.rule_id == "clang-error"
    assert parse_clang_diagnostics("") == []


def test_parse_relativises_absolute_paths(tmp_path):
    base = tmp_path / "snap"
    (base / "src").mkdir(parents=True)
    text = f"{base}/src/a.c:3:4: error: boom\n/usr/include/stdio.h:1:1: warning: sys [-Wfoo]\n./src/b.c:1:2: error: x\n"
    ds = parse_clang_diagnostics(text, base_dir=base)
    assert [d.location.file for d in ds] == ["src/a.c", "/usr/include/stdio.h", "src/b.c"]


# --------------------------------------------------------------------------- command


def test_compile_command_shape(tmp_path):
    ctx = make_project(
        tmp_path,
        {"src/a.c": CLEAN, "include/a.h": HEADER},
        compile={"flags": ["-Wall", "-O1"], "defines": ["FOO=1", "-DBAR"]},
        language={"name": "c", "standard": "c11"},
    )
    ctx.tools = Toolchain(clang="/opt/clang/bin/clang", cbmc=None, clang_version="clang version 18", cbmc_version=None)
    obj = ctx.build_dir / "obj" / "src" / "a.o"
    argv = compile_command(ctx, "src/a.c", obj)
    assert argv == [
        "/opt/clang/bin/clang",
        "-std=c11",
        "-Wall",
        "-O1",
        "-DFOO=1",
        "-DBAR",
        "-Iinclude",
        "-fno-color-diagnostics",
        "-fno-caret-diagnostics",
        "-fdiagnostics-show-option",
        "-c",
        "src/a.c",
        "-o",
        os.path.abspath(obj),
    ]
    ctx.tools = dataclasses.replace(ctx.tools, clang=None)
    with pytest.raises(ValueError):
        compile_command(ctx, "src/a.c", obj)


# --------------------------------------------------------------------------- stage


def test_clean_example_passes(needs_clang, example_copy, tmp_path):
    root = example_copy("safe_array_get")
    spec = load_spec(root / "spec.yaml")
    ctx = make_context(spec, root, tmp_path / "run")
    res = run_compile(ctx)
    assert res.stage == "compile"
    assert res.outcome is Outcome.PASS, res.summary
    assert res.status is None
    assert res.failures == []
    assert (ctx.build_dir / "obj" / "src" / "safe_array.o").is_file()
    d = res.details
    assert d["compiler"] and "clang" in d["compiler_version"]
    assert d["errors"] == 0 and d["warnings"] == 0 and d["warnings_policy"] == "report"
    [f] = d["files"]
    assert f["source"] == "src/safe_array.c" and "src/safe_array.c" in f["argv"] and f["exit_code"] == 0
    assert f["object"] == "build/obj/src/safe_array.o"
    [inv] = res.invocations
    assert inv.tool == "clang" and inv.exit_code == 0
    assert (ctx.run_dir / inv.stderr_path).is_file()


def test_syntax_error_gives_compile_error(needs_clang, tmp_path):
    ctx = make_project(tmp_path, {"src/a.c": SYNTAX_ERROR, "include/a.h": HEADER})
    res = run_compile(ctx)
    assert res.outcome is Outcome.FAIL
    assert res.status is Status.COMPILE_ERROR
    errs = [d for d in res.diagnostics if d.severity == "error"]
    assert errs, res.diagnostics
    e = errs[0]
    assert e.tool == "clang"
    assert (e.location.file, e.location.line, e.location.column) == ("src/a.c", 4, 14)
    assert "expected ';'" in e.message
    assert "src/a.c:4:14" in res.summary
    assert len(res.failures) == len(errs)
    fr = res.failures[0]
    assert fr.kind == "compile" and fr.tool == "clang" and fr.property_id == "clang-error"
    assert fr.location.line == 4
    assert fr.raw_ref and (ctx.run_dir / fr.raw_ref).is_file()
    assert "expected ';'" in (ctx.run_dir / fr.raw_ref).read_text()
    assert res.details["errors"] == len(errs)
    assert not (ctx.build_dir / "obj" / "src" / "a.o").exists()


def test_warnings_report_policy_passes(needs_clang, tmp_path):
    ctx = make_project(tmp_path, {"src/a.c": WARNING, "include/a.h": HEADER})
    res = run_compile(ctx)
    assert res.outcome is Outcome.PASS, res.summary
    [w] = [d for d in res.diagnostics if d.severity == "warning"]
    assert w.rule_id == "-Wunused-variable"
    assert (w.location.file, w.location.line, w.location.column) == ("src/a.c", 4, 9)
    assert res.failures == []
    assert res.details["warnings"] == 1 and res.details["warnings_policy"] == "report"


def test_warnings_block_policy_fails(needs_clang, tmp_path):
    ctx = make_project(tmp_path, {"src/a.c": WARNING, "include/a.h": HEADER}, compile={"warnings_policy": "block"})
    res = run_compile(ctx)
    assert res.outcome is Outcome.FAIL
    assert res.status is Status.COMPILE_ERROR
    assert "blocked by policy" in res.summary
    [fr] = res.failures
    assert fr.kind == "compile" and fr.property_id == "-Wunused-variable" and fr.location.line == 4
    # The object was still produced (the code compiles; policy blocks it).
    assert (ctx.build_dir / "obj" / "src" / "a.o").is_file()


def test_werror_flag_turns_warning_into_error(needs_clang, tmp_path):
    ctx = make_project(tmp_path, {"src/a.c": WARNING, "include/a.h": HEADER}, compile={"flags": ["-Wall", "-Werror"]})
    res = run_compile(ctx)
    assert res.outcome is Outcome.FAIL and res.status is Status.COMPILE_ERROR
    [e] = [d for d in res.diagnostics if d.severity == "error"]
    assert e.rule_id == "-Wunused-variable"


def test_error_in_header_and_multiple_files(needs_clang, tmp_path):
    bad_header = "#ifndef A_H\n#define A_H\nint f(int x)\n#endif\n"
    ctx = make_project(
        tmp_path,
        {"src/a.c": CLEAN, "src/b.c": "int g(void) { return 2; }\n", "include/a.h": bad_header},
        editable=["src/b.c", "src/a.c"],
    )
    res = run_compile(ctx)
    assert res.outcome is Outcome.FAIL
    [e] = [d for d in res.diagnostics if d.severity == "error"]
    assert (e.location.file, e.location.line) == ("include/a.h", 3)  # header error, relative path
    assert e.raw.startswith("In file included from src/a.c:1:")
    # the good file was still compiled
    assert (ctx.build_dir / "obj" / "src" / "b.o").is_file()
    assert [f["source"] for f in res.details["files"]] == ["src/b.c", "src/a.c"]


def test_missing_compiler_is_error(tmp_path):
    ctx = make_project(tmp_path, {"src/a.c": CLEAN, "include/a.h": HEADER})
    ctx.tools = dataclasses.replace(ctx.tools, clang=None, clang_version=None)
    res = run_compile(ctx)
    assert res.outcome is Outcome.ERROR
    assert res.status is Status.TOOL_ERROR
    assert "compiler not found" in res.summary
    assert res.invocations == []


def test_non_clang_compiler_is_error(tmp_path):
    ctx = make_project(tmp_path, {"src/a.c": CLEAN, "include/a.h": HEADER})
    ctx.tools = dataclasses.replace(ctx.tools, clang="/usr/bin/gcc", clang_version="gcc (Ubuntu 13.2.0) 13.2.0")
    res = run_compile(ctx)
    assert res.outcome is Outcome.ERROR and "does not identify as clang" in res.summary


def test_missing_source_fails(needs_clang, tmp_path):
    ctx = make_project(tmp_path, {"src/a.c": CLEAN, "include/a.h": HEADER}, editable=["src/a.c", "src/gone.c"])
    res = run_compile(ctx)
    assert res.outcome is Outcome.FAIL and res.status is Status.COMPILE_ERROR
    assert "src/gone.c" in res.summary
    [fr] = res.failures
    assert fr.property_id == "missing-source" and fr.location.file == "src/gone.c"
    assert "missing from the verification snapshot" in fr.description
    assert res.details["missing_sources"] == ["src/gone.c"]
    assert (ctx.build_dir / "obj" / "src" / "a.o").is_file()


def test_timeout(tmp_path):
    ctx = make_project(tmp_path, {"src/a.c": CLEAN, "include/a.h": HEADER})
    ctx.tools = dataclasses.replace(
        ctx.tools, clang=fake_tool(tmp_path, "clang", "exec sleep 30"), clang_version="fake clang version 0"
    )
    ctx.options.timeout_scale = 0.0001  # -> the 1 s floor of ctx.timeout()
    res = run_compile(ctx)
    assert res.outcome is Outcome.TIMEOUT
    assert res.status is Status.TIMEOUT
    assert "timed out" in res.summary
    assert res.invocations[0].timed_out


def test_compiler_crash_is_error(tmp_path):
    ctx = make_project(tmp_path, {"src/a.c": CLEAN, "include/a.h": HEADER})
    script = (
        "echo 'src/a.c:1:1: error: fake' >&2\n"
        "echo 'PLEASE submit a bug report to https://example.invalid' >&2\n"
        "echo 'clang: error: clang frontend command failed due to signal (use -v to see invocation)' >&2\n"
        "exit 1"
    )
    ctx.tools = dataclasses.replace(ctx.tools, clang=fake_tool(tmp_path, "clang", script), clang_version="fake clang 0")
    res = run_compile(ctx)
    assert res.outcome is Outcome.ERROR, res.summary
    assert "crashed" in res.summary
    assert res.failures == []  # a compiler crash is not attributed to the code


def test_signal_and_unexplained_exit_are_errors(tmp_path):
    ctx = make_project(tmp_path, {"src/a.c": CLEAN, "include/a.h": HEADER})
    ctx.tools = dataclasses.replace(ctx.tools, clang=fake_tool(tmp_path, "clang", "exit 3"), clang_version="fake clang 0")
    res = run_compile(ctx)
    assert res.outcome is Outcome.ERROR
    assert "exited with code 3" in res.summary

    ctx2 = make_project(tmp_path / "two", {"src/a.c": CLEAN, "include/a.h": HEADER})
    ctx2.tools = dataclasses.replace(
        ctx2.tools, clang=fake_tool(tmp_path, "clang2", "kill -SEGV $$"), clang_version="fake clang 0"
    )
    res2 = run_compile(ctx2)
    assert res2.outcome is Outcome.ERROR
    assert "signal 11" in res2.summary  # direct signal or bwrap's 128+N exit status


def test_success_without_object_is_error(tmp_path):
    ctx = make_project(tmp_path, {"src/a.c": CLEAN, "include/a.h": HEADER})
    ctx.tools = dataclasses.replace(ctx.tools, clang=fake_tool(tmp_path, "clang", "exit 0"), clang_version="fake clang 0")
    res = run_compile(ctx)
    assert res.outcome is Outcome.ERROR
    assert "no object file" in res.summary


# --------------------------------------------------------------------------- regressions


def test_parse_include_stack_of_note_stays_with_the_note():
    text = "\n".join(
        [
            "src/a.c:9:1: error: conflicting types for 'f'",
            "In file included from src/a.c:1:",
            "In file included from include/b.h:2:",
            "                 from include/c.h:3:",
            "include/a.h:3:5: note: previous declaration is here",
            "src/a.c:12:1: warning: next one [-Wfoo]",
        ]
    )
    e, w = parse_clang_diagnostics(text)
    assert e.path[0].location.file == "include/a.h"
    assert "                 from include/c.h:3:" in e.raw and "In file included from include/b.h:2:" in e.raw
    assert w.raw == "src/a.c:12:1: warning: next one [-Wfoo]"  # no leaked include context


def test_parse_diagnostic_quoting_linker_text_is_a_compiler_diagnostic():
    [d] = parse_clang_diagnostics("src/a.c:3:2: error: undefined reference to `x' [-W#warnings]\n")
    assert d.rule_id == "-W#warnings" and (d.location.file, d.location.line) == ("src/a.c", 3)


def test_crash_markers_in_source_diagnostics_are_not_a_crash(needs_clang, tmp_path):
    src = CLEAN + "#warning PLEASE submit a bug report\n#warning Stack dump:\n"
    ctx = make_project(tmp_path, {"src/a.c": src, "include/a.h": HEADER}, compile={"flags": ["-Wall"]})
    res = run_compile(ctx)
    assert res.outcome is Outcome.PASS, res.summary
    assert [d.rule_id for d in res.diagnostics] == ["-W#warnings", "-W#warnings"]
    # ... and an #error carrying the marker is still the code's COMPILE_ERROR
    ctx2 = make_project(tmp_path / "two", {"src/a.c": CLEAN + "#error PLEASE submit a bug report\n", "include/a.h": HEADER})
    res2 = run_compile(ctx2)
    assert res2.outcome is Outcome.FAIL and res2.status is Status.COMPILE_ERROR, res2.summary


def test_driver_error_without_location_is_tool_error(needs_clang, tmp_path):
    ctx = make_project(tmp_path, {"src/a.c": CLEAN, "include/a.h": HEADER}, compile={"flags": ["-fno-such-flag-leanc"]})
    res = run_compile(ctx)
    assert res.outcome is Outcome.ERROR and res.status is Status.TOOL_ERROR, res.summary
    assert "not attributable to the source" in res.summary and "-fno-such-flag-leanc" in res.summary
    assert res.failures == []
    assert any(d.severity == "error" and d.location is None for d in res.diagnostics)  # still reported


def test_symlinked_run_dir_works_in_sandbox(needs_clang, tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    ctx = make_project(tmp_path / "link", {"src/a.c": CLEAN, "include/a.h": HEADER})
    res = run_compile(ctx)
    assert res.outcome is Outcome.PASS, res.summary
    assert res.details["files"][0]["object"] == "build/obj/src/a.o"
    assert (ctx.build_dir / "obj" / "src" / "a.o").is_file()
