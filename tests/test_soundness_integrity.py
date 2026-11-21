from __future__ import annotations

import shutil
import subprocess

import pytest
from integrity_support import (
    DEFAULT_FLAGS,
    DEFAULT_GATES,
    EXAMPLES,
    READ_LINE,
    WP_EXAMPLE,
    by_rule,
    integrity_gate,
    shipped,
    with_flags,
)

from leanc.backends import integrity
from leanc.backends.cbmc.compile_flags import (
    CBMC_ARCHITECTURE_DEFINES,
    cbmc_preprocessor_macro_args,
    translate_compile_options,
)
from leanc.backends.wp import builtin_macros
from leanc.backends.wp.argv import (
    MACHDEP,
    frama_c_preprocessor_macro_args,
    preprocessor_define_args,
)
from leanc.backends.wp.builtin_macros import FRAMA_C_MISSING, PRINT_BUILTIN_MACROS_ARGS
from leanc.foundation.status import Outcome, Status
from leanc.lang.c_lexer import CSource
from leanc.rules.predefined_macros import (
    GCC_MISSING,
    PREDEFINED_MACRO_DUMP_ARGS,
    MacroDefinition,
    divergent_macro_use_violations,
    divergent_macros,
    parse_predefined_macros,
)
from leanc.specs.loader import load_spec
from leanc.specs.model import CompileConfig
from leanc.toolchain.framac import find_framac

VERIFIER = "CBMC"
FINITE_MATH_READ = "    *out = arr[idx] + __FINITE_MATH_ONLY__;"
STDC_VERSION_READ = "    *out = arr[idx] + (int)(__STDC_VERSION__ / 100000000L);"
NDEBUG_READ = "#ifndef NDEBUG\n    *out = arr[idx + 1];\n#else\n    *out = arr[idx];\n#endif"
NDEBUG_USE_READ = "    *out = arr[idx] + NDEBUG - 1;"
MIRROR_PROBE = (
    "long v = __STDC_VERSION__; int a = linux; int c = __CPROVER__; int f = FOO; int u = __CHAR_UNSIGNED__;\n"
    "int lp = __LP64__; int x = __x86_64__;\n"
    "#ifdef BAR\nint bar_defined;\n#endif\n"
    "#ifdef __STRICT_ANSI__\nint strict;\n#endif\n"
)
MIRROR_FLAGS = ["-funsigned-char", "-DFOO=3", "-DBAR", "-UBAR", "-O2", "-D__LP64__=0", "-D__x86_64__=7"]
FRAMA_C_DEFINES = ["LEGACY=2", "-DPLAIN"]
WP_EXAMPLE_GATES = "gates: [compile, static_analysis, sanitizer, cbmc, wp]"
WP_ONLY_GATES = "gates: [compile, static_analysis, sanitizer, wp]"
FRAMA_C_MACHDEP_MACRO_READS = {
    name: f"#ifdef {name}\n    *out = arr[idx];\n#else\n    *out = arr[idx + 1];\n#endif"
    for name in ("unix", "linux")
}
LEGACY_MODE_READ = "#ifdef LEGACY_MODE\n    *out = arr[idx + 1];\n#else\n    *out = arr[idx];\n#endif"
MISSING_GCC = ("leanc-test-no-such-gcc",)

CLANG_DUMP = "#define __FINITE_MATH_ONLY__ 1\n#define __STDC__ 1\n#define __INT16_C(c) c\n#define __EMPTY__\n"
GCC_DUMP = "#define __FINITE_MATH_ONLY__ 0\n#define __STDC__ 1\n#define __INT16_C(c) c\n#define __GCC_ONLY__ 2\n"

def test_predefined_macro_dump_keeps_parameters_apart_from_bodies():
    parsed = parse_predefined_macros("#define F(c) c\n#define G (c) c\n#define E\n\n")
    assert parsed.unparsed == ()
    assert parsed.definitions["F"] == MacroDefinition("(c)", "c")
    assert parsed.definitions["G"] == MacroDefinition(None, "(c) c")
    assert parsed.definitions["F"] != parsed.definitions["G"]
    assert parsed.definitions["E"] == MacroDefinition(None, "")


def test_predefined_macro_dump_reports_unexpected_lines():
    assert parse_predefined_macros("#define A 1\ngarbage line\n").unparsed == ("garbage line",)


def test_divergent_macros_are_exactly_those_with_different_or_missing_definitions():
    clang = parse_predefined_macros(CLANG_DUMP).definitions
    gcc = parse_predefined_macros(GCC_DUMP).definitions
    divergent = divergent_macros(clang, gcc, VERIFIER)
    assert set(divergent) == {"__FINITE_MATH_ONLY__", "__EMPTY__", "__GCC_ONLY__"}
    assert divergent["__GCC_ONLY__"].clang is None and divergent["__EMPTY__"].gcc is None


def test_divergent_macro_use_is_rejected_in_code_but_not_in_comments_or_strings():
    divergent = divergent_macros(parse_predefined_macros(CLANG_DUMP).definitions,
                                 parse_predefined_macros(GCC_DUMP).definitions, VERIFIER)
    text = 'int f(void)\n{\n    /* __FINITE_MATH_ONLY__ */\n    const char *s = "__GCC_ONLY__";\n' \
           "    return __FINITE_MATH_ONLY__ + (s != 0);\n}\n"
    found = divergent_macro_use_violations("src/a.c", CSource(text), divergent)
    assert [(v.rule, v.line) for v in found] == [("TOOL_DIVERGENCE", 5)]
    assert "__FINITE_MATH_ONLY__" in found[0].message and "(1)" in found[0].message and "(0)" in found[0].message
    assert f"for {VERIFIER}" in found[0].message


def test_code_without_divergent_macros_is_unaffected():
    divergent = divergent_macros(parse_predefined_macros(CLANG_DUMP).definitions,
                                 parse_predefined_macros(GCC_DUMP).definitions, VERIFIER)
    assert divergent_macro_use_violations("src/a.c", CSource(shipped()), divergent) == []


def test_macro_predefined_only_under_compile_flags_is_rejected(example_copy, needs_clang, tmp_path):
    source = shipped().replace(READ_LINE, FINITE_MATH_READ)
    result = integrity_gate(example_copy, tmp_path, source, with_flags("-Wall", "-ffast-math"))
    assert result.outcome is Outcome.FAIL
    assert any("__FINITE_MATH_ONLY__" in d.message for d in by_rule(result, "TOOL_DIVERGENCE"))


def test_same_macro_use_without_divergent_flags_passes(example_copy, needs_clang, tmp_path):
    result = integrity_gate(example_copy, tmp_path, shipped().replace(READ_LINE, FINITE_MATH_READ))
    assert result.outcome is Outcome.PASS, result.summary


def test_divergent_flags_alone_do_not_reject_code_that_ignores_them(example_copy, needs_clang, tmp_path):
    result = integrity_gate(example_copy, tmp_path, spec_edits=with_flags("-Wall", "-ffast-math", "-mavx2"))
    assert result.outcome is Outcome.PASS, result.summary


def test_define_in_compile_flags_reaches_cbmc_so_it_is_no_divergence(example_copy, needs_clang, tmp_path):
    source = shipped().replace(READ_LINE, LEGACY_MODE_READ)
    result = integrity_gate(example_copy, tmp_path, source, with_flags("-Wall", "-DLEGACY_MODE"))
    assert result.outcome is Outcome.PASS, result.summary
    assert "-DLEGACY_MODE" in result.details["predefined_macros"]["argv"]["cbmc"]


def test_define_only_in_compile_flags_is_a_divergence_for_frama_c(example_copy, needs_clang, tmp_path):
    source = shipped(WP_EXAMPLE).replace(READ_LINE, LEGACY_MODE_READ)
    result = integrity_gate(example_copy, tmp_path, source, with_flags("-Wall", "-DLEGACY_MODE"), WP_EXAMPLE)
    [found] = [d for d in by_rule(result, "TOOL_DIVERGENCE") if "'LEGACY_MODE'" in d.message]
    assert "for Frama-C" in found.message and result.outcome is Outcome.FAIL


def test_define_in_compile_defines_reaches_both_verifiers(example_copy, needs_clang, tmp_path):
    source = shipped(WP_EXAMPLE).replace(READ_LINE, LEGACY_MODE_READ)
    edits = {DEFAULT_FLAGS: f"{DEFAULT_FLAGS}\n  defines: [LEGACY_MODE]"}
    result = integrity_gate(example_copy, tmp_path, source, edits, WP_EXAMPLE)
    assert not by_rule(result, "TOOL_DIVERGENCE"), result.summary


def test_cbmc_preprocessing_standard_differs_from_the_compile_gate(example_copy, needs_clang, tmp_path):
    source = shipped().replace(READ_LINE, STDC_VERSION_READ)
    result = integrity_gate(example_copy, tmp_path, source)
    [found] = by_rule(result, "TOOL_DIVERGENCE")
    assert "'__STDC_VERSION__'" in found.message and "201710L" in found.message and "201112L" in found.message


def test_cbmc_macro_environment_mirrors_cbmc_preprocessor_command():
    compile_config = CompileConfig(flags=["-funsigned-char", "-DA=1", "-UA", "-DB", "-O2"], defines=["NDEBUG", "C"])
    assert cbmc_preprocessor_macro_args(compile_config, "c99") == [
        "-D__CPROVER__", "-m64", "-funsigned-char", "-std=gnu99", "-DB", "-DC", *CBMC_ARCHITECTURE_DEFINES
    ]
    assert cbmc_preprocessor_macro_args(CompileConfig(), "c2x")[:3] == ["-D__CPROVER__", "-m64", "-std=gnu11"]


def test_missing_gcc_is_an_integrity_error_not_a_silent_pass(example_copy, needs_clang, tmp_path, monkeypatch):
    monkeypatch.setattr(integrity, "GCC_CANDIDATES", MISSING_GCC)
    result = integrity_gate(example_copy, tmp_path)
    assert result.outcome is Outcome.ERROR and result.status is Status.TOOL_ERROR
    assert GCC_MISSING in result.summary


def test_specs_without_gcc_preprocessed_gates_do_not_need_gcc(example_copy, needs_clang, tmp_path, monkeypatch):
    monkeypatch.setattr(integrity, "GCC_CANDIDATES", MISSING_GCC)
    result = integrity_gate(example_copy, tmp_path, spec_edits={DEFAULT_GATES: "gates: [compile, static_analysis, sanitizer]"})
    assert result.outcome is Outcome.PASS, result.summary
    assert result.details["predefined_macros"]["checked"] is False


def _code_lines(preprocessed: str) -> list[str]:
    return [line for line in preprocessed.splitlines() if line.strip() and not line.startswith("#")]


@pytest.mark.parametrize("standard", ["c99", "c11", "c17"])
def test_cbmc_mirror_expands_macros_exactly_like_cbmc(tmp_path, needs_cbmc, standard):
    gcc = shutil.which("gcc")
    assert gcc, "gcc is required wherever CBMC runs"
    probe = tmp_path / "probe.c"
    probe.write_text(MIRROR_PROBE)
    compile_config = CompileConfig(flags=MIRROR_FLAGS)
    translation = translate_compile_options(compile_config, standard)
    cbmc_args = [*translation.include_args, *translation.macro_args, *translation.semantic_args]
    cbmc = subprocess.run(
        ["cbmc", *cbmc_args, "--preprocess", str(probe)], capture_output=True, text=True, check=True, cwd=tmp_path
    )
    mirror = subprocess.run(
        [gcc, *cbmc_preprocessor_macro_args(compile_config, standard), "-E", str(probe)],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
    )
    expected = [line for line in _code_lines(cbmc.stdout) if not line.startswith("CBMC version")]
    assert expected and expected == _code_lines(mirror.stdout)


@pytest.mark.parametrize("read", [NDEBUG_READ, NDEBUG_USE_READ], ids=["directive", "use"])
def test_ndebug_dropped_for_cbmc_keeps_ndebug_tests_rejected(example_copy, needs_clang, tmp_path, read):
    edits = {DEFAULT_FLAGS: f"{DEFAULT_FLAGS}\n  defines: [NDEBUG]"}
    result = integrity_gate(example_copy, tmp_path, shipped().replace(READ_LINE, read), edits)
    assert result.outcome is Outcome.FAIL
    assert any("NDEBUG" in d.message for d in by_rule(result, "TOOL_DIVERGENCE")), result.summary


def _frama_c_real_macro_dump(tmp_path, frama_c: str, defines: list[str]) -> str:
    probe = tmp_path / "probe.c"
    probe.write_text("int probe;\n")
    real = tmp_path / "real.dM"
    capture = f"gcc -E -C -dM %args %1 -o {real}; gcc -E -C %args %1 -o %2"
    subprocess.run(
        [frama_c, "-machdep", MACHDEP, "-cpp-frama-c-compliant", "-cpp-command", capture,
         "-cpp-extra-args=" + " ".join(preprocessor_define_args(defines)), str(probe)],
        capture_output=True, text=True, check=True, cwd=tmp_path,
    )
    return real.read_text()


def test_frama_c_mirror_predefines_exactly_like_frama_c(tmp_path, needs_framac):
    frama_c = find_framac().frama_c
    builtin = tmp_path / "builtin_macros.h"
    printed = subprocess.run([frama_c, *PRINT_BUILTIN_MACROS_ARGS], capture_output=True, text=True, check=True)
    builtin.write_text(printed.stdout)
    spec = load_spec(EXAMPLES / WP_EXAMPLE / "spec.yaml")
    spec.compile.defines = list(FRAMA_C_DEFINES)
    mirror = subprocess.run(
        ["gcc", *frama_c_preprocessor_macro_args(spec, str(builtin)), *PREDEFINED_MACRO_DUMP_ARGS],
        capture_output=True, text=True, check=True,
    )
    real = parse_predefined_macros(_frama_c_real_macro_dump(tmp_path, frama_c, FRAMA_C_DEFINES))
    mirrored = parse_predefined_macros(mirror.stdout)
    assert real.definitions and real.definitions == mirrored.definitions
    assert {"unix", "linux", "LEGACY", "PLAIN"} <= set(mirrored.definitions)


@pytest.mark.parametrize("name", sorted(FRAMA_C_MACHDEP_MACRO_READS))
def test_frama_c_machdep_macro_branch_is_rejected_for_wp_only_specs(example_copy, tmp_path, needs_framac, name):
    source = shipped(WP_EXAMPLE).replace(READ_LINE, FRAMA_C_MACHDEP_MACRO_READS[name])
    result = integrity_gate(example_copy, tmp_path, source, {WP_EXAMPLE_GATES: WP_ONLY_GATES}, WP_EXAMPLE)
    assert result.outcome is Outcome.FAIL, result.summary
    [found] = [d for d in by_rule(result, "TOOL_DIVERGENCE") if f"'{name}'" in d.message]
    assert "for Frama-C" in found.message


def test_missing_frama_c_is_an_integrity_error_for_wp_specs(example_copy, tmp_path, needs_clang, monkeypatch):
    monkeypatch.setattr(builtin_macros, "find_framac", lambda: None)
    result = integrity_gate(example_copy, tmp_path, spec_edits={WP_EXAMPLE_GATES: WP_ONLY_GATES}, example=WP_EXAMPLE)
    assert result.outcome is Outcome.ERROR and FRAMA_C_MISSING in result.summary
