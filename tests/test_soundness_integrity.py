from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from leanc.backends import integrity
from leanc.backends.cbmc.compile_flags import (
    cbmc_preprocessor_macro_args,
    translate_compile_options,
)
from leanc.backends.clang import run_compile
from leanc.backends.integrity import run_integrity
from leanc.foundation.status import Outcome, Status
from leanc.lang.c_lexer import CSource
from leanc.rules.c_rules import RULES
from leanc.rules.cpp_rules import CPP_RULES
from leanc.rules.predefined_macros import (
    GCC_MISSING,
    MacroDefinition,
    divergent_macro_use_violations,
    divergent_macros,
    parse_predefined_macros,
)
from leanc.rules.stack_checks import (
    MAX_STACK_FRAME_BYTES,
    STACK_FRAME_RULE,
    codegen_flags,
    stack_frame_violations,
)
from leanc.specs.loader import load_spec
from leanc.specs.model import CompileConfig
from leanc.toolchain.context import VerifyOptions, make_context

EXAMPLE = "safe_array_get"
WP_EXAMPLE = "wp_safe_array_get"
VERIFIER = "CBMC"
SOURCE = "src/safe_array.c"
DEFAULT_FLAGS = "flags: [-Wall, -Wextra, -Wpedantic]"
DEFAULT_GATES = "gates: [compile, static_analysis, sanitizer, cbmc]"
READ_LINE = "    *out = arr[idx];"
HUGE_FRAME_READ = (
    "    volatile char scratch[16 * 1024 * 1024];\n"
    "    scratch[idx % sizeof scratch] = 1;\n"
    "    *out = arr[idx] + scratch[0];"
)
FINITE_MATH_READ = "    *out = arr[idx] + __FINITE_MATH_ONLY__;"
STDC_VERSION_READ = "    *out = arr[idx] + (int)(__STDC_VERSION__ / 100000000L);"
NDEBUG_READ = "#ifndef NDEBUG\n    *out = arr[idx + 1];\n#else\n    *out = arr[idx];\n#endif"
NDEBUG_USE_READ = "    *out = arr[idx] + NDEBUG - 1;"
MIRROR_PROBE = (
    "long v = __STDC_VERSION__; int a = linux; int c = __CPROVER__; int f = FOO; int u = __CHAR_UNSIGNED__;\n"
    "#ifdef BAR\nint bar_defined;\n#endif\n"
    "#ifdef __STRICT_ANSI__\nint strict;\n#endif\n"
)
MIRROR_FLAGS = ["-funsigned-char", "-DFOO=3", "-DBAR", "-UBAR", "-O2"]
LEGACY_MODE_READ = "#ifdef LEGACY_MODE\n    *out = arr[idx + 1];\n#else\n    *out = arr[idx];\n#endif"
MISSING_GCC = ("leanc-test-no-such-gcc",)

CLANG_DUMP = "#define __FINITE_MATH_ONLY__ 1\n#define __STDC__ 1\n#define __INT16_C(c) c\n#define __EMPTY__\n"
GCC_DUMP = "#define __FINITE_MATH_ONLY__ 0\n#define __STDC__ 1\n#define __INT16_C(c) c\n#define __GCC_ONLY__ 2\n"

CLANG18_FRAME_WARNING = (
    "src/a.c:3:5: warning: stack frame size (16777240) exceeds limit (1048576) in 'big' [-Wframe-larger-than]\n"
)
CLANG18_FRAME_ERROR = (
    "src/a.c:7:1: error: stack frame size (2097168) exceeds limit (1048576) in 'other' "
    "[-Werror,-Wframe-larger-than]\n"
)
CPP_FRAME_WARNING = (
    "src/a.cpp:2:5: warning: stack frame size (16777240) exceeds limit (1048576) in 'big(int)' "
    "[-Wframe-larger-than]\n"
)


def _shipped(example: str = EXAMPLE) -> str:
    return (Path(__file__).resolve().parent.parent / "examples" / example / SOURCE).read_text()


def _gate(
    example_copy,
    tmp_path: Path,
    source: str | None = None,
    spec_edits: dict[str, str] | None = None,
    example: str = EXAMPLE,
):
    root = example_copy(example)
    if source is not None:
        (root / SOURCE).write_text(source)
    spec_file = root / "spec.yaml"
    text = spec_file.read_text()
    for old, new in (spec_edits or {}).items():
        assert old in text, old
        text = text.replace(old, new)
    spec_file.write_text(text)
    ctx = make_context(load_spec(spec_file), root, tmp_path / "run", VerifyOptions())
    compiled = run_compile(ctx)
    assert compiled.outcome is Outcome.PASS, compiled.summary
    return run_integrity(ctx)


def _flags(*flags: str) -> dict[str, str]:
    return {DEFAULT_FLAGS: f"flags: [{', '.join(flags)}]"}


def _by_rule(result, rule: str) -> list:
    return [d for d in result.diagnostics if d.rule_id == rule]


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
    assert divergent_macro_use_violations("src/a.c", CSource(_shipped()), divergent) == []


@pytest.mark.parametrize(
    "flags, kept",
    [
        (["-Wall", "-Wextra", "-O2", "-mavx2"], ["-O2", "-mavx2"]),
        (["-w", "--no-warnings", "-Werror", "-ffast-math"], ["-ffast-math"]),
        (["-Xclang", "-w", "-Xclang", "-fno-builtin", "-g"], ["-Xclang", "-fno-builtin", "-g"]),
        (["-Wl,-z,now", "-Wp,-DX=1", "-Wa,--noexecstack"], ["-Wl,-z,now", "-Wp,-DX=1", "-Wa,--noexecstack"]),
    ],
)
def test_codegen_flags_drop_only_warning_controls(flags, kept):
    assert codegen_flags(flags) == kept


def test_stack_frame_diagnostics_become_violations_with_function_and_size():
    found = stack_frame_violations("src/a.c", CLANG18_FRAME_WARNING + CLANG18_FRAME_ERROR)
    assert [(v.rule, v.line) for v in found] == [(STACK_FRAME_RULE, 3), (STACK_FRAME_RULE, 7)]
    assert "'big'" in found[0].message and "16777240" in found[0].message
    assert str(MAX_STACK_FRAME_BYTES) in found[0].message
    assert "'other'" in found[1].message and "2097168" in found[1].message


def test_cpp_stack_frame_diagnostic_keeps_the_signature():
    [found] = stack_frame_violations("src/a.cpp", CPP_FRAME_WARNING)
    assert "'big(int)'" in found.message and found.line == 2


def test_frame_in_another_file_is_reported_without_a_foreign_line():
    [found] = stack_frame_violations("src/b.c", CLANG18_FRAME_WARNING)
    assert found.file == "src/b.c" and found.line is None


def test_unrelated_diagnostics_are_not_stack_frame_violations():
    assert stack_frame_violations("src/a.c", "src/a.c:1:1: warning: unused variable 'x' [-Wunused-variable]\n") == []


def test_stack_frame_rule_is_registered_for_c_and_cpp():
    assert STACK_FRAME_RULE in RULES and STACK_FRAME_RULE in CPP_RULES
    assert MAX_STACK_FRAME_BYTES == 1048576


def test_huge_fixed_size_stack_array_is_rejected(example_copy, needs_clang, tmp_path):
    result = _gate(example_copy, tmp_path, _shipped().replace(READ_LINE, HUGE_FRAME_READ))
    assert result.outcome is Outcome.FAIL and result.status is Status.STATIC_ANALYSIS_WARNING
    [frame] = _by_rule(result, STACK_FRAME_RULE)
    assert "'safe_array_get'" in frame.message and frame.location.file == SOURCE and frame.location.line


def test_warning_suppression_in_compile_flags_cannot_hide_a_huge_frame(example_copy, needs_clang, tmp_path):
    source = _shipped().replace(READ_LINE, HUGE_FRAME_READ)
    result = _gate(example_copy, tmp_path, source, _flags("-w", "-Wno-frame-larger-than"))
    assert _by_rule(result, STACK_FRAME_RULE), result.summary


def test_shipped_code_passes_with_the_frame_and_macro_checks(example_copy, needs_clang, tmp_path):
    result = _gate(example_copy, tmp_path)
    assert result.outcome is Outcome.PASS, result.summary
    [entry] = result.details["files"]
    assert "stack" in entry["checks"]
    frame_argv = entry["argv"]["stack_frames"]
    assert f"-Wframe-larger-than={MAX_STACK_FRAME_BYTES}" in frame_argv and "-Wall" not in frame_argv
    assert result.details["predefined_macros"]["checked"] is True


def test_macro_predefined_only_under_compile_flags_is_rejected(example_copy, needs_clang, tmp_path):
    source = _shipped().replace(READ_LINE, FINITE_MATH_READ)
    result = _gate(example_copy, tmp_path, source, _flags("-Wall", "-ffast-math"))
    assert result.outcome is Outcome.FAIL
    assert any("__FINITE_MATH_ONLY__" in d.message for d in _by_rule(result, "TOOL_DIVERGENCE"))


def test_same_macro_use_without_divergent_flags_passes(example_copy, needs_clang, tmp_path):
    result = _gate(example_copy, tmp_path, _shipped().replace(READ_LINE, FINITE_MATH_READ))
    assert result.outcome is Outcome.PASS, result.summary


def test_divergent_flags_alone_do_not_reject_code_that_ignores_them(example_copy, needs_clang, tmp_path):
    result = _gate(example_copy, tmp_path, spec_edits=_flags("-Wall", "-ffast-math", "-mavx2"))
    assert result.outcome is Outcome.PASS, result.summary


def test_define_in_compile_flags_reaches_cbmc_so_it_is_no_divergence(example_copy, needs_clang, tmp_path):
    source = _shipped().replace(READ_LINE, LEGACY_MODE_READ)
    result = _gate(example_copy, tmp_path, source, _flags("-Wall", "-DLEGACY_MODE"))
    assert result.outcome is Outcome.PASS, result.summary
    assert "-DLEGACY_MODE" in result.details["predefined_macros"]["argv"]["cbmc"]


def test_define_only_in_compile_flags_is_a_divergence_for_frama_c(example_copy, needs_clang, tmp_path):
    source = _shipped(WP_EXAMPLE).replace(READ_LINE, LEGACY_MODE_READ)
    result = _gate(example_copy, tmp_path, source, _flags("-Wall", "-DLEGACY_MODE"), WP_EXAMPLE)
    [found] = [d for d in _by_rule(result, "TOOL_DIVERGENCE") if "'LEGACY_MODE'" in d.message]
    assert "for Frama-C" in found.message and result.outcome is Outcome.FAIL


def test_define_in_compile_defines_reaches_both_verifiers(example_copy, needs_clang, tmp_path):
    source = _shipped(WP_EXAMPLE).replace(READ_LINE, LEGACY_MODE_READ)
    edits = {DEFAULT_FLAGS: f"{DEFAULT_FLAGS}\n  defines: [LEGACY_MODE]"}
    result = _gate(example_copy, tmp_path, source, edits, WP_EXAMPLE)
    assert not _by_rule(result, "TOOL_DIVERGENCE"), result.summary


def test_cbmc_preprocessing_standard_differs_from_the_compile_gate(example_copy, needs_clang, tmp_path):
    source = _shipped().replace(READ_LINE, STDC_VERSION_READ)
    result = _gate(example_copy, tmp_path, source)
    [found] = _by_rule(result, "TOOL_DIVERGENCE")
    assert "'__STDC_VERSION__'" in found.message and "201710L" in found.message and "201112L" in found.message


def test_cbmc_macro_environment_mirrors_cbmc_preprocessor_command():
    compile_config = CompileConfig(flags=["-funsigned-char", "-DA=1", "-UA", "-DB", "-O2"], defines=["NDEBUG", "C"])
    assert cbmc_preprocessor_macro_args(compile_config, "c99") == [
        "-D__CPROVER__", "-funsigned-char", "-std=gnu99", "-DB", "-DC"
    ]
    assert cbmc_preprocessor_macro_args(CompileConfig(), "c2x")[:2] == ["-D__CPROVER__", "-std=gnu11"]


def test_missing_gcc_is_an_integrity_error_not_a_silent_pass(example_copy, needs_clang, tmp_path, monkeypatch):
    monkeypatch.setattr(integrity, "GCC_CANDIDATES", MISSING_GCC)
    result = _gate(example_copy, tmp_path)
    assert result.outcome is Outcome.ERROR and result.status is Status.TOOL_ERROR
    assert GCC_MISSING in result.summary


def test_specs_without_gcc_preprocessed_gates_do_not_need_gcc(example_copy, needs_clang, tmp_path, monkeypatch):
    monkeypatch.setattr(integrity, "GCC_CANDIDATES", MISSING_GCC)
    result = _gate(example_copy, tmp_path, spec_edits={DEFAULT_GATES: "gates: [compile, static_analysis, sanitizer]"})
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
    result = _gate(example_copy, tmp_path, _shipped().replace(READ_LINE, read), edits)
    assert result.outcome is Outcome.FAIL
    assert any("NDEBUG" in d.message for d in _by_rule(result, "TOOL_DIVERGENCE")), result.summary
