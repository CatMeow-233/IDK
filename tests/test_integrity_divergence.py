from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from leanc.backends.clang import run_compile
from leanc.backends.integrity import run_integrity
from leanc.foundation.status import Outcome, Status
from leanc.rules.c_rules import preprocessed_violations
from leanc.rules.source import source_violations
from leanc.specs.loader import load_spec
from leanc.toolchain.context import VerifyOptions, make_context

DIVERGENCE = Path(__file__).resolve().parent / "fixtures" / "integrity" / "divergence"
EDITABLE = "src/t.c"
AFTER_EXPANSION = "after macro expansion"
CBMC_PINS = DIVERGENCE / "cbmc_pins.c"
CLANG_PIN_FLAGS = ("-std=c17", "-w")
CBMC_DIVERGENT_ASSERTIONS = 6
FAILURE = "FAILURE"
HEADER = '#include "t.h"\n'


@pytest.mark.parametrize(
    "snippet, rule, token",
    [
        ("int f(long x) { return _Generic(x, long: 1, long long: 2); }", "TOOL_DIVERGENCE", "_Generic"),
        ("int f(void) { return __builtin_types_compatible_p(int (*)(int), int (*)()); }", "TOOL_DIVERGENCE",
         "__builtin_types_compatible_p"),
        ("int f(_Bool b) { return __builtin_classify_type(b); }", "TOOL_DIVERGENCE", "__builtin_classify_type"),
        ("typedef unsigned small_t __attribute__((mode(QI)));", "TOOL_DIVERGENCE", "mode"),
        ("typedef unsigned small_t __attribute__((__mode__(__QI__)));", "TOOL_DIVERGENCE", "mode"),
        ("static void g(int *p) { *p = 0; }\nvoid f(void) { __attribute__((cleanup(g))) int x = 0; (void)x; }",
         "CONSTRUCTOR", "cleanup"),
        ("static void g(int *p) { *p = 0; }\nvoid f(void) { __attribute__((__cleanup__(g))) int x = 0; (void)x; }",
         "CONSTRUCTOR", "cleanup"),
        ("static void g(int *p) { *p = 0; }\nvoid f(void) { __attribute__((unused, cleanup(g))) int x = 0; }",
         "CONSTRUCTOR", "cleanup"),
        ("static void g(int *p) { *p = 0; }\nvoid f(void) { __attribute ((cleanup (g))) int x = 0; (void)x; }",
         "CONSTRUCTOR", "cleanup"),
        ("static void g(int *p) { *p = 0; }\nvoid f(void) { [[gnu::cleanup(g)]] int x = 0; (void)x; }",
         "CONSTRUCTOR", "cleanup"),
    ],
    ids=["generic", "types_compatible_p", "classify_type", "mode", "mode_dunder", "cleanup", "cleanup_dunder",
         "cleanup_in_list", "cleanup_single_keyword", "cleanup_standard_attribute"],
)
def test_type_selection_and_hidden_call_constructs_are_integrity_violations(snippet, rule, token):
    spec = load_spec(DIVERGENCE / "spec.yaml")
    found = [v for v in source_violations(spec, EDITABLE, HEADER + snippet + "\n") if v.rule == rule]
    assert found and any(token in v.message for v in found), found


@pytest.mark.parametrize(
    "expanded, rule",
    [
        ("int f(unsigned long x) { return _Generic((x), unsigned long: 2u, default: 1u); }", "TOOL_DIVERGENCE"),
        ("void f(void) { __attribute__((cleanup(g))) int x = 0; }", "CONSTRUCTOR"),
    ],
    ids=["generic_from_a_header_macro", "pasted_cleanup"],
)
def test_the_expanded_view_catches_the_constructs_from_macros(expanded, rule):
    pp = f'# 1 "{EDITABLE}"\n# 1 "include/t.h" 1\nint t_fill(void);\n# 2 "{EDITABLE}" 2\n{expanded}\n'
    found = [v for v in preprocessed_violations(EDITABLE, pp) if v.rule == rule]
    assert found and all(AFTER_EXPANSION in v.message and v.line == 2 for v in found), found


def test_the_c_keyword_generic_inside_a_string_or_comment_is_not_a_violation():
    spec = load_spec(DIVERGENCE / "spec.yaml")
    text = HEADER + '/* _Generic cleanup */\nstatic const char *name = "_Generic __builtin_classify_type";\n'
    assert source_violations(spec, EDITABLE, text) == []


def _integrity(tmp_path: Path, variant: str | None):
    root = tmp_path / "proj"
    shutil.copytree(DIVERGENCE, root, ignore=shutil.ignore_patterns("variants", "cbmc_pins.c"))
    if variant is not None:
        shutil.copyfile(DIVERGENCE / "variants" / variant, root / EDITABLE)
    ctx = make_context(load_spec(root / "spec.yaml"), root, tmp_path / "run", VerifyOptions(sandbox_preference="none"))
    compiled = run_compile(ctx)
    assert compiled.outcome is Outcome.PASS, compiled.summary
    return run_integrity(ctx)


@pytest.mark.parametrize(
    "variant, rule, expanded",
    [
        ("generic.c", "TOOL_DIVERGENCE", False),
        ("header_generic.c", "TOOL_DIVERGENCE", True),
        ("types_compatible.c", "TOOL_DIVERGENCE", False),
        ("classify_type.c", "TOOL_DIVERGENCE", False),
        ("mode.c", "TOOL_DIVERGENCE", False),
        ("cleanup.c", "CONSTRUCTOR", False),
        ("cleanup_pasted.c", "CONSTRUCTOR", True),
    ],
)
def test_the_integrity_gate_rejects_code_whose_verified_model_differs_from_the_build(
    needs_clang, tmp_path, variant, rule, expanded
):
    result = _integrity(tmp_path, variant)
    assert result.outcome is Outcome.FAIL and result.status is Status.STATIC_ANALYSIS_WARNING, result.summary
    found = [d for d in result.diagnostics if d.rule_id == rule]
    assert found and all(d.location and d.location.file == EDITABLE for d in found), result.summary
    assert any(AFTER_EXPANSION in d.message for d in found) is expanded


def test_the_unmodified_project_passes_integrity_although_its_header_defines_a_generic_macro(needs_clang, tmp_path):
    result = _integrity(tmp_path, None)
    assert result.outcome is Outcome.PASS, [d.message for d in result.diagnostics]


def _cbmc_results(tmp_path: Path) -> list[dict]:
    cbmc = shutil.which("cbmc")
    assert cbmc
    proc = subprocess.run([cbmc, str(CBMC_PINS), "--json-ui"], capture_output=True, text=True, cwd=tmp_path, check=False)
    messages = json.loads(proc.stdout)
    return [result for message in messages if isinstance(message, dict) for result in message.get("result", [])]


def test_cbmc_and_clang_disagree_on_every_rejected_construct(needs_clang, needs_cbmc, tmp_path):
    clang = shutil.which("clang")
    assert clang
    binary = tmp_path / "pins"
    subprocess.run([clang, *CLANG_PIN_FLAGS, str(CBMC_PINS), "-o", str(binary)], check=True, capture_output=True)
    assert subprocess.run([str(binary)], check=False).returncode == 0
    assertions = [result for result in _cbmc_results(tmp_path) if ".assertion." in result.get("property", "")]
    assert len(assertions) == CBMC_DIVERGENT_ASSERTIONS
    assert all(result.get("status") == FAILURE for result in assertions), assertions
