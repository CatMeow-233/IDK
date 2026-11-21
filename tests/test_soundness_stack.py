from __future__ import annotations

import pytest
from integrity_support import READ_LINE, SOURCE, by_rule, integrity_gate, shipped, with_flags

from leanc.foundation.status import Outcome, Status
from leanc.rules.c_rules import RULES
from leanc.rules.cpp_rules import CPP_RULES
from leanc.rules.stack_checks import (
    MAX_STACK_FRAME_BYTES,
    STACK_FRAME_RULE,
    UNBOUNDED_STACK_RULE,
    codegen_flags,
    stack_usage_findings,
)

HUGE_FRAME_READ = (
    "    volatile char scratch[16 * 1024 * 1024];\n"
    "    scratch[idx % sizeof scratch] = 1;\n"
    "    *out = arr[idx] + scratch[0];"
)
VLA_UNDER_FLAG_READ = (
    "#ifdef USE_VLA\n    int scratch[len + 1];\n    scratch[0] = arr[idx];\n    *out = scratch[0];\n"
    "#else\n    *out = arr[idx];\n#endif"
)
STACK_USAGE = "src/a.c:3:big\t16777240\tstatic\nsrc/a.c:7:other\t2097168\tdynamic,bounded\nsrc/a.c:9:small\t64\tstatic\n"
CPP_STACK_USAGE = "src/a.cpp:2:_Z3bigi\t1999896\tstatic\n"
FLAGS_THAT_HID_THE_FRAME_DIAGNOSTIC = [
    ["-w", "-Wno-frame-larger-than"],
    ["-fno-show-column"],
    ["-fno-show-source-location"],
    ["-fdiagnostics-format=msvc"],
    ["-fmessage-length=80"],
    ["-flto"],
    ["-Werror"],
]


@pytest.mark.parametrize(
    "flags, kept",
    [
        (["-Wall", "-Wextra", "-O2", "-mavx2"], ["-O2", "-mavx2"]),
        (["-w", "--no-warnings", "-Werror", "-ffast-math"], ["-ffast-math"]),
        (["-pedantic", "-pedantic-errors", "-g", "-fno-show-column"], ["-g", "-fno-show-column"]),
    ],
)
def test_codegen_flags_drop_only_warning_controls(flags, kept):
    assert codegen_flags(flags) == kept


def test_stack_usage_rows_above_the_limit_become_violations_with_function_and_size():
    usage = stack_usage_findings("src/a.c", STACK_USAGE)
    assert usage.unparsed == []
    assert [(v.rule, v.line) for v in usage.violations] == [(STACK_FRAME_RULE, 3), (STACK_FRAME_RULE, 7)]
    assert "'big'" in usage.violations[0].message and "16777240" in usage.violations[0].message
    assert str(MAX_STACK_FRAME_BYTES) in usage.violations[0].message
    assert "'other'" in usage.violations[1].message and "2097168" in usage.violations[1].message


def test_cpp_stack_usage_names_the_symbol():
    [found] = stack_usage_findings("src/a.cpp", CPP_STACK_USAGE).violations
    assert "'_Z3bigi'" in found.message and found.line == 2


def test_frame_in_another_file_is_reported_without_a_foreign_line():
    [found] = stack_usage_findings("src/b.c", STACK_USAGE).violations[:1]
    assert found.file == "src/b.c" and found.line is None


def test_unreadable_stack_usage_rows_are_reported_not_skipped():
    usage = stack_usage_findings("src/a.c", "src/a.c:3:big 16777240 static\n")
    assert usage.violations == [] and usage.unparsed == ["src/a.c:3:big 16777240 static"]


def test_stack_frame_rule_is_registered_for_c_and_cpp():
    assert STACK_FRAME_RULE in RULES and STACK_FRAME_RULE in CPP_RULES
    assert MAX_STACK_FRAME_BYTES == 1048576


def test_huge_fixed_size_stack_array_is_rejected(example_copy, needs_clang, tmp_path):
    result = integrity_gate(example_copy, tmp_path, shipped().replace(READ_LINE, HUGE_FRAME_READ))
    assert result.outcome is Outcome.FAIL and result.status is Status.STATIC_ANALYSIS_WARNING
    [frame] = by_rule(result, STACK_FRAME_RULE)
    assert "'safe_array_get'" in frame.message and frame.location.file == SOURCE and frame.location.line


@pytest.mark.parametrize("flags", FLAGS_THAT_HID_THE_FRAME_DIAGNOSTIC, ids=" ".join)
def test_compile_flags_cannot_hide_a_huge_frame(example_copy, needs_clang, tmp_path, flags):
    source = shipped().replace(READ_LINE, HUGE_FRAME_READ)
    result = integrity_gate(example_copy, tmp_path, source, with_flags("-Wall", *flags))
    assert result.outcome is Outcome.FAIL and by_rule(result, STACK_FRAME_RULE), result.summary


def test_flags_that_stop_code_generation_make_the_frame_check_an_error(example_copy, needs_clang, tmp_path):
    result = integrity_gate(example_copy, tmp_path, spec_edits=with_flags("-Wall", "-emit-llvm"))
    assert result.outcome is Outcome.ERROR and result.status is Status.TOOL_ERROR
    assert "produced no stack usage" in result.summary
    [entry] = result.details["files"]
    assert "stack" not in entry["checks"]


def test_vla_enabled_by_a_compile_flags_define_is_rejected(example_copy, needs_clang, tmp_path):
    source = shipped().replace(READ_LINE, VLA_UNDER_FLAG_READ)
    result = integrity_gate(example_copy, tmp_path, source, with_flags("-Wall", "-DUSE_VLA"))
    assert result.outcome is Outcome.FAIL and by_rule(result, UNBOUNDED_STACK_RULE), result.summary


def test_shipped_code_passes_with_the_frame_check(example_copy, needs_clang, tmp_path):
    result = integrity_gate(example_copy, tmp_path)
    assert result.outcome is Outcome.PASS, result.summary
    [entry] = result.details["files"]
    assert "stack" in entry["checks"]
    frame_argv = entry["argv"]["stack_frames"]
    assert "-fstack-usage" in frame_argv and "-fno-lto" in frame_argv and "-Wall" not in frame_argv
