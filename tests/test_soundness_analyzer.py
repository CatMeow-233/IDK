from __future__ import annotations

from pathlib import Path

from leanc.backends.analyzer import analyzer_command, run_static_analysis
from leanc.backends.clang import DIAGNOSTIC_FLAGS
from leanc.foundation.status import Outcome, Status
from leanc.specs.model import Spec
from leanc.toolchain.context import BackendContext, make_context
from leanc.toolchain.environment import Toolchain

HEADER = "#ifndef A_H\n#define A_H\nint f(int x);\n#endif\n"
NULL_DEREF_ONLY_IN_LEGACY_MODE = """#include <stddef.h>
#include "a.h"
int f(int x)
{
#ifdef LEGACY_MODE
    int *q = NULL;
    if (x == 42) {
        return *q;
    }
#endif
    return x;
}
"""
LEGACY_FLAGS = ["-Wall", "-DLEGACY_MODE"]
FAKE_CLANG = "/x/clang"


def _project(tmp_path: Path, flags: list[str], defines: list[str] | None = None) -> BackendContext:
    root = tmp_path / "proj"
    for rel, content in {"src/a.c": NULL_DEREF_ONLY_IN_LEGACY_MODE, "include/a.h": HEADER}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    spec = Spec.model_validate(
        {
            "spec_id": "analyzer-flags",
            "version": 1,
            "title": "analyzer flags",
            "requirement": "test",
            "target": {"function": "f", "signature": "int f(int x)", "returns": {"type": "int"}},
            "sources": {"editable": ["src/a.c"], "headers": ["include/a.h"], "include_dirs": ["include"]},
            "compile": {"flags": flags, "defines": defines or []},
            "gates": ["compile", "static_analysis"],
        }
    )
    return make_context(spec, root, tmp_path / "run")


def test_analyzer_argv_carries_compile_flags_and_defines_like_compile(tmp_path):
    ctx = _project(tmp_path, ["-ffast-math", "-DLEGACY_MODE"], defines=["N=3"])
    ctx.tools = Toolchain(clang=FAKE_CLANG, cbmc=None, clang_version="clang version 18", cbmc_version=None)
    for argv in (analyzer_command(ctx, "src/a.c", tmp_path / "o.sarif"),
                 analyzer_command(ctx, "src/a.c", None, output_format="text")):
        flags_at = argv.index("-ffast-math")
        assert argv[flags_at + 1] == "-DLEGACY_MODE"
        assert argv.index("-std=c17") < flags_at < argv.index("-DN=3") < argv.index(DIAGNOSTIC_FLAGS[0])
        assert argv[-1] == "src/a.c"


def test_analyzer_sees_code_enabled_only_by_compile_flags(tmp_path, needs_clang):
    result = run_static_analysis(_project(tmp_path, LEGACY_FLAGS))
    assert result.outcome is Outcome.FAIL, result.summary
    assert result.status is Status.STATIC_ANALYSIS_WARNING
    assert any(d.rule_id == "core.NullDereference" for d in result.diagnostics)


def test_analyzer_without_those_flags_finds_nothing(tmp_path, needs_clang):
    result = run_static_analysis(_project(tmp_path, ["-Wall"]))
    assert result.outcome is Outcome.PASS, result.summary
    assert result.diagnostics == []
