from __future__ import annotations

import dataclasses

import pytest
from integrity_support import READ_LINE, SOURCE, by_rule, integrity_gate, shipped, with_flags

from leanc.backends import stack_check
from leanc.foundation.status import Outcome, Status
from leanc.rules.c_rules import RULES
from leanc.rules.cpp_rules import CPP_RULES
from leanc.rules.stack_checks import (
    DIAGNOSTIC_LAYOUT_FLAGS,
    MAX_STACK_FRAME_BYTES,
    STACK_FRAME_RULE,
    UNBOUNDED_STACK_RULE,
    UnboundedStackScan,
    codegen_flags,
    stack_usage_findings,
)
from leanc.toolchain.context import BackendContext

HUGE_FRAME_READ = (
    "    volatile char scratch[16 * 1024 * 1024];\n"
    "    scratch[idx % sizeof scratch] = 1;\n"
    "    *out = arr[idx] + scratch[0];"
)
VLA_UNDER_FLAG_READ = (
    "#ifdef USE_VLA\n    int scratch[len + 1];\n    scratch[0] = arr[idx];\n    *out = scratch[0];\n"
    "#else\n    *out = arr[idx];\n#endif"
)
STACK_USAGE = "src/a.c:3:big\t16777240\tstatic\nsrc/a.c:7:other\t2097168\tstatic\nsrc/a.c:9:small\t64\tstatic\n"
CPP_STACK_USAGE = "src/a.cpp:2:_Z3bigi\t1999896\tstatic\nsrc/a.cpp:__clang_call_terminate\t8\tstatic\n"
GENERATED_HUGE_FRAME = "src/a.cpp:__generated_thunk\t2000000\tstatic\n"
OPTIMISED_AWAY_FRAME_READ = (
    "    char scratch[2 * 1024 * 1024];\n    scratch[idx % sizeof scratch] = 1;\n"
    "    *out = arr[idx] + scratch[idx % sizeof scratch] - 1;"
)
VLA_READ = "    int scratch[len + 1];\n    scratch[0] = arr[idx];\n    *out = scratch[0];"
VARIABLY_MODIFIED_READ = "    const int (*view)[len] = (const int (*)[len])arr;\n    *out = (*view)[idx];"
DATA_ONLY_FILE = "src/table.c"
DATA_ONLY_SOURCE = '#include "safe_array.h"\nstatic const int unused_table[4] = {1, 2, 3, 4};\n'
CPP_EXAMPLE = "cpp_ring_buffer"
CPP_NO_CODE_FILE = "src/templates_only.cpp"
CPP_NO_CODE_SOURCE = "template <typename T> T twice(T v) { return v + v; }\ninline int unused_inline() { return 1; }\n"
LIMIT_ROWS = (
    f"src/a.c:3:at_limit\t{MAX_STACK_FRAME_BYTES}\tstatic\n"
    f"src/a.c:5:over_limit\t{MAX_STACK_FRAME_BYTES + 1}\tstatic\n"
)
SIGKILL = 9
LAYOUT_FLAGS_THE_VLA_CHECK_OVERRIDES = [
    "-fno-show-column",
    "-fno-show-source-location",
    "-fdiagnostics-format=msvc",
    "-fmessage-length=80",
    "-fno-diagnostics-show-option",
    "-fcolor-diagnostics",
    "-fdiagnostics-print-source-range-info",
    "-fdiagnostics-show-category=name",
]
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


def test_cpp_stack_usage_names_the_symbol_and_accepts_rows_without_a_line():
    usage = stack_usage_findings("src/a.cpp", CPP_STACK_USAGE)
    assert usage.unparsed == []
    [found] = usage.violations
    assert "'_Z3bigi'" in found.message and found.line == 2


def test_huge_frame_of_a_generated_function_has_no_line():
    [found] = stack_usage_findings("src/a.cpp", GENERATED_HUGE_FRAME).violations
    assert "'__generated_thunk'" in found.message and found.line is None


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


def test_frame_limit_is_exclusive():
    [found] = stack_usage_findings("src/a.c", LIMIT_ROWS).violations
    assert "'over_limit'" in found.message and found.line == 5


def test_frame_is_measured_without_optimisation(example_copy, needs_clang, tmp_path):
    source = shipped().replace(READ_LINE, OPTIMISED_AWAY_FRAME_READ)
    result = integrity_gate(example_copy, tmp_path, source, with_flags("-Wall", "-O2"))
    assert result.outcome is Outcome.FAIL and by_rule(result, STACK_FRAME_RULE), result.summary
    [entry] = result.details["files"]
    assert "-O0" in entry["argv"]["stack_frames"]


@pytest.mark.parametrize("flag", LAYOUT_FLAGS_THE_VLA_CHECK_OVERRIDES)
def test_diagnostic_layout_flags_cannot_turn_a_vla_into_a_tool_error(example_copy, needs_clang, tmp_path, flag):
    result = integrity_gate(example_copy, tmp_path, shipped().replace(READ_LINE, VLA_READ), with_flags("-Wall", flag))
    assert result.outcome is Outcome.FAIL and by_rule(result, UNBOUNDED_STACK_RULE), result.summary
    assert set(DIAGNOSTIC_LAYOUT_FLAGS) <= set(result.details["files"][0]["argv"]["stack"])


def test_unparsed_failing_vla_check_is_an_error(example_copy, needs_clang, tmp_path, monkeypatch):
    monkeypatch.setattr(stack_check, "unbounded_stack_scan", lambda stderr, reason, project_root, tolerated_dirs: UnboundedStackScan())
    result = integrity_gate(example_copy, tmp_path, shipped().replace(READ_LINE, VARIABLY_MODIFIED_READ))
    assert result.outcome is Outcome.ERROR and "stack check of src/safe_array.c failed (exit 1)" in result.summary
    assert "stack" not in result.details["files"][0]["checks"]


def _killed_when(marker: str):
    original = BackendContext.run

    def run(self, argv, **kwargs):
        proc = original(self, argv, **kwargs)
        return dataclasses.replace(proc, exit_code=None, signal=SIGKILL, stderr="") if marker in argv else proc

    return run


@pytest.mark.parametrize("marker", ["-fsyntax-only", "-fstack-usage"])
def test_stack_check_killed_by_a_signal_is_not_a_pass(example_copy, needs_clang, tmp_path, monkeypatch, marker):
    monkeypatch.setattr(BackendContext, "run", _killed_when(marker))
    result = integrity_gate(example_copy, tmp_path)
    assert result.outcome is Outcome.ERROR and "exit None" in result.summary, result.summary
    assert "stack" not in result.details["files"][0]["checks"]


def test_file_without_functions_passes_the_frame_check(example_copy, needs_clang, tmp_path):
    edits = {f"editable: [{SOURCE}]": f"editable: [{SOURCE}, {DATA_ONLY_FILE}]"}
    result = integrity_gate(example_copy, tmp_path, spec_edits=edits, extra_files={DATA_ONLY_FILE: DATA_ONLY_SOURCE})
    assert result.outcome is Outcome.PASS, result.summary
    assert all("stack" in entry["checks"] for entry in result.details["files"])


def test_cpp_file_without_emitted_functions_passes_the_frame_check(example_copy, needs_clang, tmp_path):
    edits = {"editable: [src/ring_buffer.cpp]": f"editable: [src/ring_buffer.cpp, {CPP_NO_CODE_FILE}]"}
    result = integrity_gate(
        example_copy, tmp_path, spec_edits=edits, example=CPP_EXAMPLE, extra_files={CPP_NO_CODE_FILE: CPP_NO_CODE_SOURCE}
    )
    assert result.outcome is Outcome.PASS, result.summary


def test_side_outputs_of_the_stack_checks_stay_in_the_build_dir(example_copy, needs_clang, tmp_path):
    result = integrity_gate(example_copy, tmp_path, spec_edits=with_flags("-Wall", "-save-temps=obj"))
    assert result.outcome is Outcome.PASS, result.summary
    [entry] = result.details["files"]
    syntax_output = entry["argv"]["stack"][entry["argv"]["stack"].index("-o") + 1]
    assert syntax_output.endswith(".syntax") and "/stack-usage/" in syntax_output
    assert not list((tmp_path / "run" / "snapshot").rglob("*.i"))
    assert list((tmp_path / "run" / "build" / "stack-usage").glob("*.i"))
