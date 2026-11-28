from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from leanc.backends import integrity
from leanc.backends.clang import run_compile
from leanc.backends.integrity import run_integrity
from leanc.foundation.status import Outcome, Status
from leanc.lang.c_lexer import CSource
from leanc.repair.policy import check_candidate, check_generated
from leanc.rules.c_names import ALLOWED_IMPORTS
from leanc.rules.c_rules import (
    RULES,
    check_object_symbols,
    parse_nm,
    preprocessed_violations,
    section_violations,
)
from leanc.rules.definitions import defined_functions
from leanc.rules.source import source_violations
from leanc.specs.loader import load_spec
from leanc.toolchain.context import VerifyOptions, make_context
from leanc.toolchain.framac import MIN_FRAMAC_MAJOR, find_framac, framac_version_key
from leanc.toolchain.tools import tool_version

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
FIX = Path(__file__).resolve().parent / "fixtures" / "integrity"

SAFE = ("safe_array_get", "src/safe_array.c")
RATIO = ("div_by_zero", "src/safe_ratio.c")

REPROS = [
    ("cand_assertfail.c", SAFE, {"SYMBOL_OVERRIDE"}),
    ("cand_nondet.c", SAFE, {"SYMBOL_OVERRIDE", "CPROVER_BUILTIN"}),
    ("cand_pragma.c", SAFE, {"OBFUSCATION"}),
    ("cand_pragma2.c", SAFE, {"OBFUSCATION"}),
    ("cand_clang.c", SAFE, {"TOOL_DIVERGENCE"}),
    ("cand_opt.c", SAFE, {"TOOL_DIVERGENCE"}),
    ("cand_exit.c", SAFE, {"TERMINATION"}),
    ("cand_vac_sa.c", SAFE, {"TERMINATION"}),
    ("cand_leak.c", SAFE, {"CONSTRUCTOR", "TERMINATION"}),
    ("cand_atexit.c", SAFE, {"CONSTRUCTOR", "TERMINATION"}),
    ("cand_vacmacro.c", SAFE, {"TOOL_DIVERGENCE", "TERMINATION"}),
    ("reqs_cheat.c", SAFE, {"TOOL_DIVERGENCE", "CONSTRUCTOR", "SYMBOL_OVERRIDE", "TERMINATION"}),
    ("reqs_div.c", SAFE, {"TOOL_DIVERGENCE"}),
    ("sec_af.c", RATIO, {"SYMBOL_OVERRIDE"}),
    ("sec_a1_src.c", RATIO, {"TOOL_DIVERGENCE"}),
    ("sec_a2_src.c", RATIO, {"SYMBOL_OVERRIDE", "CPROVER_BUILTIN"}),
    ("sec_a3.c", RATIO, {"SYMBOL_OVERRIDE", "CONSTRUCTOR", "TERMINATION"}),
    ("sec_vf.c", RATIO, {"CPROVER_BUILTIN"}),
    ("sec_ab.c", RATIO, {"TERMINATION"}),
    ("sec_a4.c", ("null_deref", "src/sensor.c"), {"LINE_DIRECTIVE"}),
    ("sec_a5.c", ("runtime_memory", "src/join.c"), {"CONSTRUCTOR", "TERMINATION"}),
]
GATE_EXTRA = {
    "cand_nondet.c": {"DANGEROUS_IMPORT"},
    "sec_a2_src.c": {"DANGEROUS_IMPORT"},
    "sec_vf.c": {"DANGEROUS_IMPORT"},
    "cand_pragma.c": {"PRAGMA"},
    "cand_pragma2.c": {"PRAGMA"},
}
CONTROLS = [("cand_leak_ctl.c", SAFE), ("sec_af_noovr.c", RATIO), ("sec_a2_bug_only.c", RATIO)]


def _gate(example_copy, tmp_path: Path, example: str, files: dict[str, str]):
    root = example_copy(example)
    for rel, text in files.items():
        (root / rel).write_text(text)
    spec = load_spec(root / "spec.yaml")
    ctx = make_context(spec, root, tmp_path / "run", VerifyOptions())
    compiled = run_compile(ctx)
    assert compiled.outcome is Outcome.PASS, compiled.summary
    return run_integrity(ctx), spec, ctx


def _rules(result) -> set[str]:
    return {d.rule_id for d in result.diagnostics}


def _shipped(example: str, rel: str) -> str:
    return (EXAMPLES / example / rel).read_text()


@pytest.mark.parametrize("fixture, where, expected", REPROS, ids=[r[0] for r in REPROS])
def test_repro_fails_the_gate(example_copy, needs_clang, tmp_path, fixture, where, expected):
    example, rel = where
    res, _, _ = _gate(example_copy, tmp_path, example, {rel: (FIX / fixture).read_text()})
    assert res.stage == "integrity"
    assert res.outcome is Outcome.FAIL, res.summary
    assert res.status is Status.STATIC_ANALYSIS_WARNING
    assert expected | GATE_EXTRA.get(fixture, set()) <= _rules(res), (res.summary, [d.message for d in res.diagnostics])
    for d in res.diagnostics:
        assert d.tool == "leanc-integrity" and d.severity == "error" and d.rule_id in RULES and d.message
    for f in res.failures:
        assert f.kind == "policy" and f.tool == "leanc-integrity" and f.property_id in RULES
    located = [d for d in res.diagnostics if d.rule_id in expected]
    assert located and all(d.location and d.location.file == rel for d in located)
    assert any(d.location.line for d in located)
    assert len({f.id for f in res.failures}) == len(res.failures)


@pytest.mark.parametrize("fixture, where, expected", REPROS, ids=[r[0] for r in REPROS])
def test_repro_rejected_by_policy(fixture, where, expected):
    example, rel = where
    spec = load_spec(EXAMPLES / example / "spec.yaml")
    text = (FIX / fixture).read_text()
    d = check_candidate(spec, {rel: _shipped(example, rel)}, {rel: text})
    assert not d.allowed and expected <= {v.rule for v in d.violations}, d.violations
    g = check_generated(spec, {rel: text})
    assert not g.allowed and expected <= {v.rule for v in g.violations}
    assert expected <= {v.rule for v in source_violations(spec, rel, text)}


@pytest.mark.parametrize("fixture, where", CONTROLS, ids=[c[0] for c in CONTROLS])
def test_honest_buggy_controls_pass_the_gate(example_copy, needs_clang, tmp_path, fixture, where):
    example, rel = where
    res, spec, _ = _gate(example_copy, tmp_path, example, {rel: (FIX / fixture).read_text()})
    assert res.outcome is Outcome.PASS, [d.message for d in res.diagnostics]
    assert source_violations(spec, rel, (FIX / fixture).read_text()) == []


def _accepted_rounds(ex: Path) -> list[dict[str, str]]:
    out = []
    for r in sorted((ex / "mock" / "repair").glob("round-*.json")):
        if ex.name == "cheating_repair" and r.name in {f"round-{i}.json" for i in range(1, 5)}:
            continue
        files = json.loads(r.read_text()).get("files") or {}
        if files:
            out.append(files)
    return out


def _legit_sets() -> list[tuple[str, str, dict[str, str]]]:
    sets = []
    for ex in sorted(p for p in EXAMPLES.iterdir() if (p / "spec.yaml").is_file()):
        spec = load_spec(ex / "spec.yaml")
        src = {rel: (ex / rel).read_text() for rel in spec.sources.editable if (ex / rel).is_file()}
        gen = {rel: (ex / "mock/generate" / rel).read_text() for rel in spec.sources.editable
               if (ex / "mock/generate" / rel).is_file()}
        if src:
            sets.append((f"{ex.name}:src", ex.name, src))
        if gen:
            sets.append((f"{ex.name}:generate", ex.name, gen))
        for k, files in enumerate(_accepted_rounds(ex), 1):
            sets.append((f"{ex.name}:repair{k}", ex.name, {**(gen or src), **files}))
    return sets


LEGIT = _legit_sets()


def test_legit_corpus_is_not_empty():
    names = {n.split(":")[0] for n, _, _ in LEGIT}
    assert len(LEGIT) >= 30 and {"safe_array_get", "ring_buffer", "cheating_repair", "div_by_zero"} <= names


def _has_usable_framac() -> bool:
    fc = find_framac()
    return fc is not None and framac_version_key(tool_version(fc.real, "-version"))[0] >= MIN_FRAMAC_MAJOR


@pytest.mark.parametrize("name, example, files", LEGIT, ids=[n for n, _, _ in LEGIT])
def test_shipped_and_accepted_code_passes_gate_and_policy(example_copy, needs_clang, tmp_path, name, example, files):
    if "wp" in load_spec(EXAMPLES / example / "spec.yaml").gates and not _has_usable_framac():
        pytest.skip(f"Frama-C >= {MIN_FRAMAC_MAJOR} not installed")
    res, spec, _ = _gate(example_copy, tmp_path, example, files)
    assert res.outcome is Outcome.PASS, (res.summary, [d.message for d in res.diagnostics])
    for rel, text in files.items():
        assert source_violations(spec, rel, text) == [], rel
    gen = check_generated(spec, files)
    assert gen.allowed, gen.violations
    if ":repair" in name:
        base = {rel: (EXAMPLES / example / "mock/generate" / rel).read_text() for rel in files}
        if base != files:
            d = check_candidate(spec, base, files)
            assert d.allowed, d.violations


def test_pass_details_record_what_was_checked(example_copy, needs_clang, tmp_path):
    res, _, _ = _gate(example_copy, tmp_path, "ring_buffer", {})
    assert res.outcome is Outcome.PASS
    files = {f["source"]: f for f in res.details["files"]}
    assert set(files) == {"src/ring.c", "src/ring_stats.c"}
    section_tools = {"readelf", "otool", "llvm-readobj", "objdump"}
    for f in files.values():
        assert f["checks"] == ["source", "preprocessed", "stack", "symbols", "sections"]
        assert {"preprocess", "nm"} <= set(f["argv"])
        assert section_tools & set(f["argv"])
        assert "-E" in f["argv"]["preprocess"]
    assert "ring_push" in files["src/ring.c"]["defined"]
    assert res.details["tools"]["nm"]
    if sys.platform.startswith("linux"):
        assert res.details["tools"]["readelf"]
    assert res.invocations and all(i.tool in ({"clang", "gcc", "nm"} | section_tools) for i in res.invocations)


def test_macro_built_termination_is_seen_after_expansion(example_copy, needs_clang, tmp_path):
    text = _shipped(*SAFE).replace(
        '#include "safe_array.h"\n', '#include <stdlib.h>\n#include "safe_array.h"\n#define QUIT(c) ex##it(c)\n'
    ).replace("    *out = arr[idx];", "    if (len == 7) QUIT(0);\n    *out = arr[idx];")
    assert "TERMINATION" not in {v.rule for v in source_violations(load_spec(EXAMPLES / SAFE[0] / "spec.yaml"), SAFE[1], text)}
    res, _, _ = _gate(example_copy, tmp_path, SAFE[0], {SAFE[1]: text})
    term = [d for d in res.diagnostics if d.rule_id == "TERMINATION"]
    assert res.outcome is Outcome.FAIL and term
    assert any("after macro expansion" in d.message for d in term)
    assert any("imports 'exit'" in d.message for d in term)


def test_object_level_symbol_rules(example_copy, needs_clang, tmp_path):
    text = _shipped(*SAFE).replace(
        '#include "safe_array.h"\n',
        '#include <stdlib.h>\n#include "safe_array.h"\nint helper_value = 3;\n'
        "static int parse(const char *s) { return (int)strtol(s, 0, 10); }\n",
    ).replace("    *out = arr[idx];", '    *out = arr[idx] + parse("0") + helper_value - 3;')
    res, _, _ = _gate(example_copy, tmp_path, SAFE[0], {SAFE[1]: text})
    msgs = {d.rule_id: d.message for d in res.diagnostics}
    assert res.outcome is Outcome.FAIL
    assert "helper_value" in msgs["SYMBOL_OVERRIDE"] and "make it static" in msgs["SYMBOL_OVERRIDE"]
    assert "strtol" in msgs["DANGEROUS_IMPORT"] and "allowlist" in msgs["DANGEROUS_IMPORT"]
    assert all(d.location.line for d in res.diagnostics)


def test_missing_target_definition(example_copy, needs_clang, tmp_path):
    res, _, _ = _gate(example_copy, tmp_path, SAFE[0], {SAFE[1]: '#include "safe_array.h"\n'})
    assert res.outcome is Outcome.FAIL and _rules(res) == {"MISSING_TARGET"}


def test_missing_tools_are_tool_errors_not_passes(example_copy, needs_clang, tmp_path, monkeypatch):
    root = example_copy(SAFE[0])
    spec = load_spec(root / "spec.yaml")
    ctx = make_context(spec, root, tmp_path / "run", VerifyOptions())
    assert run_compile(ctx).outcome is Outcome.PASS
    monkeypatch.setattr(integrity, "find_tool", lambda name, env_var=None: None)
    res = run_integrity(ctx)
    assert res.outcome is Outcome.ERROR and res.status is Status.TOOL_ERROR
    assert "nm" in res.summary
    assert any(tool in res.summary for tool in ("readelf", "otool", "llvm-readobj"))
    (ctx.snapshot_dir / SAFE[1]).write_text((FIX / "cand_exit.c").read_text())
    res = run_integrity(ctx)
    assert res.outcome is Outcome.FAIL and "TERMINATION" in _rules(res) and "could not run" in res.summary


def test_missing_object_is_a_tool_error(example_copy, needs_clang, tmp_path):
    root = example_copy(SAFE[0])
    spec = load_spec(root / "spec.yaml")
    ctx = make_context(spec, root, tmp_path / "run", VerifyOptions())
    res = run_integrity(ctx)
    assert res.outcome is Outcome.ERROR and "compile gate" in res.summary


@pytest.fixture(scope="module")
def spec():
    return load_spec(EXAMPLES / "safe_array_get" / "spec.yaml")


HEADERS = {"include/safe_array.h": (EXAMPLES / "safe_array_get/include/safe_array.h").read_text()}


def _src_rules(spec, text: str, **kw) -> set[str]:
    return {v.rule for v in source_violations(spec, "src/safe_array.c", text, **kw)}


@pytest.mark.parametrize(
    "snippet, rule",
    [
        ("#ifdef __clang__\nint a;\n#endif", "TOOL_DIVERGENCE"),
        ("#if defined(__OPTIMIZE__) || 0\nint a;\n#endif", "TOOL_DIVERGENCE"),
        ("#ifndef NDEBUG\nint a;\n#endif", "TOOL_DIVERGENCE"),
        ("#if defined(LEANC_VACUITY_CHECK)\nint a;\n#endif", "TOOL_DIVERGENCE"),
        ("#if __has_include(<stdio.h>)\nint a;\n#endif", "TOOL_DIVERGENCE"),
        ("#if X\nint a;\n#elif defined(_FORTIFY_SOURCE)\nint b;\n#endif", "TOOL_DIVERGENCE"),
        ("#define IS_CLANG __clang__", "TOOL_DIVERGENCE"),
        ("#undef __GNUC__", "TOOL_DIVERGENCE"),
        ("#define NDEBUG 1", "TOOL_DIVERGENCE"),
        ("#ident \"x\"", "TOOL_DIVERGENCE"),
        ("#assert machine(x86)", "TOOL_DIVERGENCE"),
        ("int v = __clang_major__;", "TOOL_DIVERGENCE"),
        ("int s(int x) { return __builtin_constant_p(x); }", "TOOL_DIVERGENCE"),
        ("#pragma GCC diagnostic ignored \"-Wall\"", "PRAGMA"),
        ("#pragma CPROVER check disable \"bounds\"", "PRAGMA"),
        ("#pragma clang system_header", "PRAGMA"),
        ("_Pragma(\"CPROVER check push\")", "PRAGMA"),
        ("#line 1 \"include/safe_array.h\"", "LINE_DIRECTIVE"),
        ("# 1 \"src/safe_array.c\" 3", "LINE_DIRECTIVE"),
        ("#define assert(x) ((void)0)", "SYMBOL_OVERRIDE"),
        ("#undef malloc", "SYMBOL_OVERRIDE"),
        ("#define while if", "SYMBOL_OVERRIDE"),
        ("#define safe_array_get(a, b, c, d) 0", "SYMBOL_OVERRIDE"),
        ("void __assert_fail(const char *a, const char *b, unsigned c, const char *d) { (void)a; }", "SYMBOL_OVERRIDE"),
        ("int nondet_int(void) { return 0; }", "SYMBOL_OVERRIDE"),
        ("int main(void) { return 0; }", "SYMBOL_OVERRIDE"),
        ("void *memcpy(void *d, const void *s, unsigned long n) { return d; }", "SYMBOL_OVERRIDE"),
        ("static void free(void *p) { (void)p; }", "SYMBOL_OVERRIDE"),
        ("extern int any(void) __attribute__((weak));", "SYMBOL_OVERRIDE"),
        ("int y __attribute__((alias(\"x\")));", "SYMBOL_OVERRIDE"),
        ("#define P(x) _Pragma(#x)", "OBFUSCATION"),
        ("_Pragma(STR)", "OBFUSCATION"),
        ("#include \"../harness/safe_array_get_harness.c\"", "INCLUDE_ESCAPE"),
        ("#include \"/etc/passwd\"", "INCLUDE_ESCAPE"),
        ("#include \"leanc.h\"", "INCLUDE_ESCAPE"),
        ("#include <leanc.h>", "INCLUDE_ESCAPE"),
        ("#include \"stdio.h\"", "INCLUDE_ESCAPE"),
        ("#include <safe_array_get_harness.c>", "INCLUDE_ESCAPE"),
        ("#include <test_safe_array_get.c>", "INCLUDE_ESCAPE"),
        ("#include HDR", "INCLUDE_ESCAPE"),
        ("#include_next <stdio.h>", "INCLUDE_ESCAPE"),
        ("#embed \"data.bin\"", "INCLUDE_ESCAPE"),
        ("int f(void) { return __CPROVER_rounding_mode; }", "CPROVER_BUILTIN"),
        ("void f(int x) { __VERIFIER_assume(x); }", "CPROVER_BUILTIN"),
        ("int f(void) { return nondet_int(); }", "CPROVER_BUILTIN"),
        ("void f(void) { __asm__(\"nop\"); }", "INLINE_ASM"),
        ("void f(int x) { if (x) __builtin_unreachable(); }", "ASSUMPTION_INJECTION"),
        ("_Noreturn static void die(void) { for (;;) {} }", "ASSUMPTION_INJECTION"),
        ("__attribute__((noreturn)) static void die(void);", "ASSUMPTION_INJECTION"),
        ("__attribute__((nonnull)) static int g(int *p);", "ASSUMPTION_INJECTION"),
        ("__attribute__((__constructor__)) static void c(void) {}", "CONSTRUCTOR"),
        ("[[gnu::destructor]] static void d(void) {}", "CONSTRUCTOR"),
        ("__attribute__((section(\".init_array\"))) static void (*p)(void);", "CONSTRUCTOR"),
        ("__attribute__((no_sanitize(\"address\"))) static int h(void);", "SANITIZER_SUPPRESSION"),
        ("__attribute__((optimize(\"O0\"))) static int h(void);", "TOOL_DIVERGENCE"),
        ("void f(void) { exit(0); }", "TERMINATION"),
        ("void f(void) { _exit(0); }", "TERMINATION"),
        ("void f(void) { abort(); }", "TERMINATION"),
        ("void f(void (*g)(void)) { atexit(g); }", "TERMINATION"),
        ("void f(void) { void (*q)(int) = exit; (void)q; }", "TERMINATION"),
        ("void f(void) { __builtin_trap(); }", "TERMINATION"),
        ("void f(void *b) { longjmp(b, 1); }", "TERMINATION"),
        ("void f(void) { raise(9); }", "TERMINATION"),
        ("void f(void) { fork(); }", "DANGEROUS_IMPORT"),
        ("void f(void) { pthread_create(0, 0, 0, 0); }", "DANGEROUS_IMPORT"),
        ("void f(void) { __lsan_disable(); }", "DANGEROUS_IMPORT"),
    ],
)
def test_source_rule(spec, snippet, rule):
    text = '#include "safe_array.h"\n' + snippet + "\n"
    assert rule in _src_rules(spec, text), source_violations(spec, "src/safe_array.c", text)
    v = next(v for v in source_violations(spec, "src/safe_array.c", text) if v.rule == rule)
    assert v.file == "src/safe_array.c" and 2 <= v.line <= 2 + snippet.count("\n")


def test_header_macro_redefinition_needs_headers(spec):
    text = '#include "safe_array.h"\n#undef SAFE_ARRAY_ERR_RANGE\n#define SAFE_ARRAY_ERR_RANGE (-1)\n'
    assert "SYMBOL_OVERRIDE" not in _src_rules(spec, text)
    assert "SYMBOL_OVERRIDE" in _src_rules(spec, text, headers=HEADERS)


@pytest.mark.parametrize(
    "snippet",
    [
        "/* #ifdef __clang__  exit(0); __CPROVER_assume(x); #pragma GCC diagnostic */",
        "// #include \"/etc/passwd\"  _Pragma(#x)  __attribute__((constructor))",
        'static const char *msg = "exit(0) abort() #pragma once __clang__ nondet_int()";',
        "#if 0\n#ifdef __clang__\nvoid g(void) { exit(0); }\n#endif\n#endif",
        "#define LEN(a) (sizeof(a) / sizeof((a)[0]))\n#define MAX_ITEMS 8\n#ifdef MAX_ITEMS\n#endif",
        "#include <stdlib.h>\n#include <assert.h>\n#include <string.h>\n#include <safe_array.h>",
        "#pragma once",
        "struct ops { void (*exit)(int); };\nstatic void call(struct ops *o) { o->exit(1); }",
        "static int exiting = 0, aborted = 1, signals = 2; static int use(void) { return exiting + aborted + signals; }",
        "static int f(int x) { assert(x > 0); return __LINE__ + (int)sizeof(__func__); }",
        "__attribute__((unused)) static int g(int x) { return x; }",
    ],
)
def test_source_rules_have_no_false_positives(spec, snippet):
    text = '#include "safe_array.h"\n#include <assert.h>\n' + snippet + "\n"
    assert source_violations(spec, "src/safe_array.c", text, headers=HEADERS) == []


def test_preprocessed_scan_on_synthetic_output():
    pp = (
        '# 1 "src/x.c"\n# 1 "include/x.h" 1\n__attribute__((constructor)) void hdr(void);\n'
        '# 2 "src/x.c" 2\nint f(void) {\n#pragma CPROVER check disable "bounds"\n  return 0;\n}\n'
        '# 10 "src/x.c"\nvoid g(void) { exit(0); }\n'
    )
    vs = preprocessed_violations("src/x.c", pp)
    got = {(v.rule, v.line) for v in vs}
    assert got == {("PRAGMA", 3), ("TERMINATION", 10)}, vs
    assert all("after macro expansion" in v.message for v in vs)


def test_check_object_symbols_unit(spec):
    syms = parse_nm(
        "safe_array_get T 0000000000000000 0000000000000064\n"
        "helper T 0000000000000070 10\n"
        "local_fn t 0000000000000090 10\n"
        "__assert_fail W 00000000000000a0 10\n"
        "memcpy U\nmalloc U\n__udivti3 U\nring_count U\n"
        "exit U\nfork U\n__lsan_disable U\nstrtol U\n__VERIFIER_assume w\nnondet_int U\n"
    )
    vs = check_object_symbols(
        spec, "src/safe_array.c", syms, header_idents={"safe_array_get", "size_t"}, defined_elsewhere={"ring_count"}
    )
    by_name = {}
    for v in vs:
        for n in ("helper", "__assert_fail", "exit", "fork", "__lsan_disable", "strtol", "__VERIFIER_assume", "nondet_int"):
            if f"'{n}'" in v.message or f" {n}:" in v.message or f"symbol {n}" in v.message:
                by_name[n] = v.rule
    assert by_name == {
        "helper": "SYMBOL_OVERRIDE",
        "__assert_fail": "SYMBOL_OVERRIDE",
        "exit": "TERMINATION",
        "fork": "DANGEROUS_IMPORT",
        "__lsan_disable": "DANGEROUS_IMPORT",
        "strtol": "DANGEROUS_IMPORT",
        "__VERIFIER_assume": "DANGEROUS_IMPORT",
        "nondet_int": "CPROVER_BUILTIN",
    }, vs
    assert len(vs) == 8
    assert {"memcpy", "malloc", "free", "__assert_fail"} <= ALLOWED_IMPORTS


def test_section_violations_unit():
    out = (
        "  [ 0]                   NULL            0000000000000000 000000 000000 00      0   0  0\n"
        "  [ 1] .text             PROGBITS        0000000000000000 000040 000010 00  AX  0   0 16\n"
        "  [ 5] .init_array       INIT_ARRAY      0000000000000000 000068 000008 08  WA  0   0  8\n"
        "  [ 6] .rela.init_array  RELA            0000000000000000 000200 000018 18   I 12   5  8\n"
        "  [ 7] .fini_array.00100 FINI_ARRAY      0000000000000000 000070 000008 08  WA  0   0  8\n"
    )
    names = [v.message.split()[3].rstrip(":") for v in section_violations("src/x.c", out)]
    assert names == [".init_array", ".rela.init_array", ".fini_array.00100"]
    assert section_violations("src/x.c", out.replace(".init_array", ".data").replace(".fini_array", ".bss")) == []


def test_policy_reports_integrity_rules_without_duplicates(spec):
    text = (FIX / "cand_vacmacro.c").read_text()
    d = check_candidate(spec, {SAFE[1]: _shipped(*SAFE)}, {SAFE[1]: text})
    keys = [(v.rule, v.file, v.line) for v in d.violations if v.line]
    assert len(keys) == len(set(keys))
    assert {"TOOL_DIVERGENCE", "TERMINATION", "CPROVER_BUILTIN"} <= {v.rule for v in d.violations}


def test_defined_functions_helper():
    src = CSource(
        "int f(int x);\nstatic int g(void) { return f(1); }\n"
        "__attribute__((constructor)) static void c(void) { }\nint (h)(void);\nint k(void) [[gnu::used]] { return 0; }\n"
    )
    assert [n for n, _ in defined_functions(src)] == ["g", "c", "k"]
