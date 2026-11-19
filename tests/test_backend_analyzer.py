"""Tests for the Clang Static Analyzer gate (leanc.backends.analyzer)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from leanc.backends import analyzer
from leanc.backends.analyzer import analyzer_command, parse_sarif, run_static_analysis
from leanc.backends.tools import Toolchain
from leanc.core.context import BackendContext, make_context
from leanc.core.status import Outcome, Status
from leanc.specs.loader import load_spec
from leanc.specs.model import Spec

# --------------------------------------------------------------------------- helpers

HEADER = """#ifndef A_H
#define A_H
int f(int x);
#endif
"""

NULL_DEREF = """#include <stddef.h>
#include "a.h"
int f(int x)
{
    int *q = NULL;
    if (x == 42) {
        return *q;
    }
    return x;
}
"""
NULL_DEREF_LINE, NULL_DEREF_COL = 7, 16

DIV_ZERO = """#include "a.h"
int f(int x)
{
    int z = 0;
    if (x > 3) {
        z = 0;
    }
    return x / z;
}
"""
DIV_ZERO_LINE = 8

CLEAN = """#include "a.h"
int f(int x)
{
    return x > 0 ? x - 1 : 0;
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
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
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


def _phys(uri: str | None = None, line: int | None = None, col: int | None = None, **art) -> dict:
    a = dict(art)
    if uri is not None:
        a["uri"] = uri
    region = {}
    if line is not None:
        region["startLine"] = line
    if col is not None:
        region["startColumn"] = col
    return {"physicalLocation": {"artifactLocation": a, "region": region}}


# --------------------------------------------------------------------------- parse_sarif


def test_parse_sarif_file_uri_relative_to_base_dir(tmp_path):
    base = tmp_path / "snap"
    (base / "src").mkdir(parents=True)
    uri = (base / "src" / "bad.c").as_uri()
    data = {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "clang", "rules": [{"id": "core.NullDereference"}]}},
                "results": [
                    {
                        "ruleId": "core.NullDereference",
                        "level": "warning",
                        "message": {"text": "Dereference of null pointer"},
                        "locations": [_phys(uri, 5, 16)],
                        "codeFlows": [
                            {
                                "threadFlows": [
                                    {
                                        "locations": [
                                            {"location": {**_phys(uri, 3, 5), "message": {"text": "'q' initialized to null"}}},
                                            {"location": {**_phys(uri, 4, 9), "message": {"text": "Assuming 'p' is NULL"}}},
                                            {"location": {**_phys(uri, 5, 16), "message": {"text": "Dereference"}}},
                                        ]
                                    }
                                ]
                            }
                        ],
                    }
                ],
            }
        ],
    }
    [d] = parse_sarif(data, base_dir=base)
    assert d.tool == "clang-static-analyzer"
    assert d.rule_id == "core.NullDereference"
    assert d.severity == "warning"
    assert d.message == "Dereference of null pointer"
    assert (d.location.file, d.location.line, d.location.column) == ("src/bad.c", 5, 16)
    assert [s.message for s in d.path] == ["'q' initialized to null", "Assuming 'p' is NULL", "Dereference"]
    assert [s.location.line for s in d.path] == [3, 4, 5]
    assert all(s.location.file == "src/bad.c" for s in d.path)
    assert d.raw and "core.NullDereference" in d.raw
    # custom tool name
    assert parse_sarif(data, base_dir=base, tool="x")[0].tool == "x"


def test_parse_sarif_uri_variants(tmp_path):
    base = tmp_path / "snap dir"
    base.mkdir()
    data = {
        "runs": [
            {
                "artifacts": [{"location": {"uri": (base / "src" / "via index.c").as_uri()}}],
                "originalUriBaseIds": {"SRCROOT": {"uri": base.as_uri() + "/"}},
                "tool": {
                    "driver": {
                        "rules": [
                            {"id": "core.DivideZero", "defaultConfiguration": {"level": "error"}},
                            {"id": "unix.Malloc"},
                        ]
                    }
                },
                "results": [
                    # absolute file:// outside base_dir: kept absolute
                    {"ruleId": "a.B", "message": {"text": "outside"}, "locations": [_phys("file:///usr/include/stdio.h", 1, 2)]},
                    # relative uri: kept relative
                    {"ruleId": "a.B", "message": {"text": "relative"}, "locations": [_phys("src/rel.c", 7)]},
                    # percent-encoded absolute uri inside base (directory name has a space)
                    {"ruleId": "a.B", "message": {"text": "encoded"}, "locations": [_phys((base / "src" / "x.c").as_uri(), 2)]},
                    # uri via artifact index; ruleId via ruleIndex; level via rule default
                    {"ruleIndex": 0, "message": {"text": "indexed"}, "locations": [_phys(None, 9, 1, index=0)]},
                    # uriBaseId resolution
                    {"ruleId": "unix.Malloc", "message": {"text": "based"}, "locations": [_phys("src/b.c", 4, uriBaseId="SRCROOT")]},
                    # file:/abs (single slash) and level none
                    {"ruleId": "a.B", "level": "none", "message": {"text": "single"}, "locations": [_phys(f"file:{base}/s.c", 1)]},
                ],
            }
        ]
    }
    ds = parse_sarif(data, base_dir=base)
    files = [d.location.file for d in ds]
    assert files == ["/usr/include/stdio.h", "src/rel.c", "src/x.c", "src/via index.c", "src/b.c", "s.c"]
    assert ds[1].location.line == 7 and ds[1].location.column is None
    assert ds[3].rule_id == "core.DivideZero" and ds[3].severity == "error"
    assert ds[5].severity == "info"
    # without base_dir absolute paths stay absolute
    assert parse_sarif(data)[2].location.file == str(base / "src" / "x.c")


def test_parse_sarif_robust_to_missing_fields():
    assert parse_sarif({}) == []
    assert parse_sarif({"runs": None}) == []
    assert parse_sarif({"runs": [None, 3, {"results": "nope"}]}) == []
    assert parse_sarif("not a dict") == []  # type: ignore[arg-type]
    ds = parse_sarif(
        {
            "runs": [
                {
                    "results": [
                        {},
                        "junk",
                        {"ruleId": "x.Y", "level": 7, "locations": [{"physicalLocation": None}], "codeFlows": [{"threadFlows": [{"locations": [{}]}]}]},
                        {"ruleId": "x.Z", "message": {"text": "m"}, "locations": [_phys(None, "12", None)]},
                        {"ruleId": "x.W", "message": {"id": "default", "arguments": ["foo", "3"]}, "ruleIndex": 99},
                    ]
                }
            ]
        }
    )
    assert len(ds) == 4  # the non-dict result is skipped, the empty one kept
    empty, partial, strline, msgid = ds
    assert empty.rule_id is None and empty.message == "static analysis finding" and empty.location is None
    assert empty.severity == "warning"
    assert partial.rule_id == "x.Y" and partial.message == "x.Y" and partial.severity == "warning"
    assert partial.location is None and len(partial.path) == 1 and partial.path[0].location is None
    assert strline.location.line == 12
    assert msgid.message == "x.W"  # id without rule messageStrings: falls back to the rule id


def test_parse_sarif_message_strings_and_logical_locations():
    data = {
        "runs": [
            {
                "tool": {
                    "driver": {
                        "rules": [
                            {"id": "my.Rule", "messageStrings": {"default": {"text": "value {0} out of range in {1}"}}}
                        ]
                    }
                },
                "results": [
                    {
                        "ruleId": "my.Rule",
                        "message": {"id": "default", "arguments": ["idx", "get"]},
                        "locations": [
                            {
                                **_phys("src/a.c", 3, 1),
                                "logicalLocations": [{"fullyQualifiedName": "get", "kind": "function"}],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    [d] = parse_sarif(data)
    assert d.message == "value idx out of range in get"
    assert d.location.function == "get" and d.location.file == "src/a.c"


# --------------------------------------------------------------------------- command


def test_analyzer_command_shape(tmp_path):
    ctx = make_project(
        tmp_path,
        {"src/a.c": CLEAN, "include/a.h": HEADER},
        compile={"defines": ["N=3"]},
        static_analysis={"extra_checkers": ["alpha.security.ArrayBoundV2"]},
    )
    ctx.tools = Toolchain(clang="/x/clang", cbmc=None, clang_version="clang version 18", cbmc_version=None)
    out = tmp_path / "o.sarif"
    argv = analyzer_command(ctx, "src/a.c", out)
    assert argv[:6] == ["/x/clang", "--analyze", "--analyzer-output", "sarif", "-o", str(out)]
    assert "-std=c17" in argv and "-DN=3" in argv and "-Iinclude" in argv
    i = argv.index("-analyzer-checker=alpha.security.ArrayBoundV2")
    assert argv[i - 1] == "-Xclang"
    assert argv[-1] == "src/a.c"
    targv = analyzer_command(ctx, "src/a.c", None, output_format="text")
    assert targv[1:4] == ["--analyze", "-Xclang", "-analyzer-output=text"]
    assert "sarif" not in targv


# --------------------------------------------------------------------------- stage


def test_clean_example_passes(needs_clang, example_copy, tmp_path):
    root = example_copy("safe_array_get")
    ctx = make_context(load_spec(root / "spec.yaml"), root, tmp_path / "run")
    res = run_static_analysis(ctx)
    assert res.stage == "static_analysis"
    assert res.outcome is Outcome.PASS, res.summary
    assert res.status is None and res.diagnostics == [] and res.failures == []
    d = res.details
    assert d["format"] == "sarif" and d["findings"] == 0 and d["policy"] == "block"
    assert "clang" in d["clang_version"]
    [f] = d["files"]
    assert "--analyze" in f["argv"] and f["argv"][-1] == "src/safe_array.c"
    raw = ctx.run_dir / f["sarif"]
    assert raw.is_file() and raw.parent == ctx.raw_dir / "static_analysis"
    assert json.loads(raw.read_text())["runs"][0]["results"] == []
    [inv] = res.invocations
    assert inv.tool == "clang-static-analyzer" and inv.exit_code == 0


def test_null_dereference_blocks(needs_clang, tmp_path):
    ctx = make_project(tmp_path, {"src/a.c": NULL_DEREF, "include/a.h": HEADER})
    res = run_static_analysis(ctx)
    assert res.outcome is Outcome.FAIL, res.summary
    assert res.status is Status.STATIC_ANALYSIS_WARNING
    [d] = res.diagnostics
    assert d.tool == "clang-static-analyzer" and d.rule_id == "core.NullDereference"
    assert (d.location.file, d.location.line, d.location.column) == ("src/a.c", NULL_DEREF_LINE, NULL_DEREF_COL)
    assert d.path, "analyzer bug path must be preserved"
    assert any("null" in s.message.lower() for s in d.path)
    assert all(s.location is None or s.location.file == "src/a.c" for s in d.path)
    [fr] = res.failures
    assert fr.kind == "static" and fr.tool == "clang-static-analyzer"
    assert fr.property_id == "core.NullDereference"
    assert fr.location.line == NULL_DEREF_LINE
    assert len(fr.trace) == len(d.path) and fr.trace[0].kind == "location"
    assert fr.raw_ref and (ctx.run_dir / fr.raw_ref).is_file()
    assert "core.NullDereference" in (ctx.run_dir / fr.raw_ref).read_text()
    assert res.details["findings"] == 1 and res.details["by_rule"] == {"core.NullDereference": 1}
    assert "core.NullDereference" in res.summary and "policy=block" in res.summary


def test_division_by_zero_found(needs_clang, tmp_path):
    ctx = make_project(tmp_path, {"src/a.c": DIV_ZERO, "include/a.h": HEADER})
    res = run_static_analysis(ctx)
    assert res.outcome is Outcome.FAIL and res.status is Status.STATIC_ANALYSIS_WARNING
    [d] = res.diagnostics
    assert d.rule_id == "core.DivideZero"
    assert d.location.line == DIV_ZERO_LINE and d.location.file == "src/a.c"
    assert d.path
    assert res.failures[0].property_id == "core.DivideZero"


def test_report_policy_passes_with_diagnostics(needs_clang, tmp_path):
    ctx = make_project(
        tmp_path,
        {"src/a.c": NULL_DEREF, "src/b.c": DIV_ZERO.replace("int f(int x)", "int g(int x)"), "include/a.h": HEADER},
        static_analysis={"policy": "report"},
    )
    res = run_static_analysis(ctx)
    assert res.outcome is Outcome.PASS, res.summary
    assert res.status is None
    assert res.failures == []
    assert sorted(d.rule_id for d in res.diagnostics) == ["core.DivideZero", "core.NullDereference"]
    assert {d.location.file for d in res.diagnostics} == {"src/a.c", "src/b.c"}
    assert "not blocking" in res.summary and res.details["findings"] == 2
    assert len(res.invocations) == 2


def test_missing_compiler_is_error(tmp_path):
    ctx = make_project(tmp_path, {"src/a.c": CLEAN, "include/a.h": HEADER})
    ctx.tools = dataclasses.replace(ctx.tools, clang=None, clang_version=None)
    res = run_static_analysis(ctx)
    assert res.outcome is Outcome.ERROR and res.status is Status.TOOL_ERROR
    assert "clang not found" in res.summary
    assert res.invocations == []


def test_text_fallback_when_sarif_unusable(needs_clang, tmp_path, monkeypatch):
    # Make the "SARIF" run produce a plist instead: the file exists but is not
    # SARIF JSON, so the stage must fall back to textual output -- and say so.
    monkeypatch.setattr(analyzer, "SARIF_ARGS", ("-Xclang", "-analyzer-output=plist"))
    ctx = make_project(tmp_path, {"src/a.c": NULL_DEREF, "include/a.h": HEADER})
    res = run_static_analysis(ctx)
    assert res.outcome is Outcome.FAIL, res.summary
    assert res.details["format"] == "text"
    assert any("fell back to textual" in n for n in res.details["notes"])
    assert "format: text" in res.summary
    [d] = res.diagnostics
    assert d.rule_id == "core.NullDereference" and d.location.line == NULL_DEREF_LINE
    assert d.path and d.path[-1].location.line == NULL_DEREF_LINE  # bug path from note: lines
    [fr] = res.failures
    assert fr.raw_ref.endswith(".analyzer.txt") and (ctx.run_dir / fr.raw_ref).is_file()
    assert len(res.invocations) == 2  # the SARIF attempt and the textual re-run are both recorded
    f = res.details["files"][0]
    assert "not valid JSON" in f["fallback_reason"] and "-analyzer-output=text" in f["text_argv"]


def test_extra_checker_enabled(needs_clang, tmp_path):
    src = '#include "a.h"\nint f(int x)\n{\n    int a[4] = {1, 2, 3, 4};\n    (void)x;\n    return a[5];\n}\n'
    ctx = make_project(
        tmp_path, {"src/a.c": src, "include/a.h": HEADER}, static_analysis={"extra_checkers": ["alpha.security.ArrayBoundV2"]}
    )
    res = run_static_analysis(ctx)
    assert res.outcome is Outcome.FAIL
    assert "alpha.security.ArrayBoundV2" in {d.rule_id for d in res.diagnostics}
    assert res.details["checkers"]["extra"] == ["alpha.security.ArrayBoundV2"]
    assert "-analyzer-checker=alpha.security.ArrayBoundV2" in res.details["files"][0]["argv"]


def test_unknown_checker_is_tool_error(needs_clang, tmp_path):
    ctx = make_project(
        tmp_path, {"src/a.c": CLEAN, "include/a.h": HEADER}, static_analysis={"extra_checkers": ["nosuch.Checker"]}
    )
    res = run_static_analysis(ctx)
    assert res.outcome is Outcome.ERROR, res.summary
    assert "nosuch.Checker" in res.summary


def test_invalid_checker_name_rejected_without_running(tmp_path):
    ctx = make_project(
        tmp_path, {"src/a.c": CLEAN, "include/a.h": HEADER}, static_analysis={"extra_checkers": ["core;rm -rf"]}
    )
    res = run_static_analysis(ctx)
    assert res.outcome is Outcome.ERROR and "invalid" in res.summary
    assert res.invocations == []


def test_code_that_does_not_compile_is_tool_error(needs_clang, tmp_path):
    ctx = make_project(tmp_path, {"src/a.c": "int f(int x) { return x }\n", "include/a.h": HEADER})
    res = run_static_analysis(ctx)
    assert res.outcome is Outcome.ERROR and res.status is Status.TOOL_ERROR
    assert "src/a.c:1:24" in res.summary
    assert any(d.severity == "error" for d in res.diagnostics)


def test_same_header_finding_from_two_units_reported_once(needs_clang, tmp_path):
    hdr = "#ifndef H_H\n#define H_H\nstatic inline int load(int *p) { return *p; }\n#endif\n"
    a = '#include <stddef.h>\n#include "h.h"\nint a(void) { return load(NULL); }\n'
    ctx = make_project(tmp_path, {"src/a.c": a, "src/b.c": a.replace("int a(", "int b("), "include/h.h": hdr})
    res = run_static_analysis(ctx)
    assert res.outcome is Outcome.FAIL
    [d] = res.diagnostics
    assert d.location.file == "include/h.h" and d.location.line == 3
    assert len(res.invocations) == 2


def test_timeout(tmp_path):
    ctx = make_project(tmp_path, {"src/a.c": CLEAN, "include/a.h": HEADER})
    ctx.tools = dataclasses.replace(
        ctx.tools, clang=fake_tool(tmp_path, "clang", "exec sleep 30"), clang_version="fake clang version 0"
    )
    ctx.options.timeout_scale = 0.0001
    res = run_static_analysis(ctx)
    assert res.outcome is Outcome.TIMEOUT and res.status is Status.TIMEOUT
    assert res.invocations[0].timed_out


def test_crash_is_tool_error(tmp_path):
    ctx = make_project(tmp_path, {"src/a.c": CLEAN, "include/a.h": HEADER})
    ctx.tools = dataclasses.replace(ctx.tools, clang=fake_tool(tmp_path, "clang", "kill -SEGV $$"), clang_version="fake clang 0")
    res = run_static_analysis(ctx)
    assert res.outcome is Outcome.ERROR and "crashed" in res.summary


def test_missing_sarif_and_failing_fallback_is_error(tmp_path):
    # A "clang" that exits 0 but writes nothing: SARIF missing -> textual
    # fallback -> which also produces nothing usable but exits 0 -> no findings,
    # recorded as format "text" with a note (not silently treated as SARIF).
    ctx = make_project(tmp_path, {"src/a.c": CLEAN, "include/a.h": HEADER})
    ctx.tools = dataclasses.replace(ctx.tools, clang=fake_tool(tmp_path, "clang", "exit 0"), clang_version="fake clang 0")
    res = run_static_analysis(ctx)
    assert res.details["format"] == "text"
    assert any("SARIF file was not produced" in n for n in res.details["notes"])
    # and when the fallback fails too, the stage is an ERROR
    ctx2 = make_project(tmp_path / "two", {"src/a.c": CLEAN, "include/a.h": HEADER})
    script = 'case "$*" in *analyzer-output=text*) echo "error: boom" >&2; exit 1;; esac\nexit 0'
    ctx2.tools = dataclasses.replace(ctx2.tools, clang=fake_tool(tmp_path, "clang2", script), clang_version="fake clang 0")
    res2 = run_static_analysis(ctx2)
    assert res2.outcome is Outcome.ERROR and "textual fallback" in res2.summary


def test_symlinked_run_dir_still_uses_sarif(needs_clang, tmp_path):
    # The sandbox binds the build dir at its real path; the SARIF output path
    # must be canonical or clang silently fails to write it.
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    ctx = make_project(tmp_path / "link", {"src/a.c": NULL_DEREF, "include/a.h": HEADER})
    res = run_static_analysis(ctx)
    assert res.outcome is Outcome.FAIL, res.summary
    assert res.details["format"] == "sarif" and res.details["notes"] == []
    [d] = res.diagnostics
    assert d.location.file == "src/a.c" and d.location.line == NULL_DEREF_LINE


def test_unwritable_sarif_fallback_reason_quotes_clang(tmp_path):
    script = 'case "$*" in *sarif*) echo "warning: could not create file: No such file or directory" >&2;; esac\nexit 0'
    ctx = make_project(tmp_path, {"src/a.c": CLEAN, "include/a.h": HEADER})
    ctx.tools = dataclasses.replace(ctx.tools, clang=fake_tool(tmp_path, "clang", script), clang_version="fake clang 0")
    res = run_static_analysis(ctx)
    assert res.details["format"] == "text"
    assert "could not create file" in res.details["files"][0]["fallback_reason"]


def test_crash_marker_in_source_is_not_an_analyzer_crash(needs_clang, tmp_path):
    src = NULL_DEREF + "#warning Stack dump:\n"
    ctx = make_project(tmp_path, {"src/a.c": src, "include/a.h": HEADER})
    res = run_static_analysis(ctx)
    assert res.outcome is Outcome.FAIL and res.status is Status.STATIC_ANALYSIS_WARNING, res.summary
    assert [d.rule_id for d in res.diagnostics] == ["core.NullDereference"]
