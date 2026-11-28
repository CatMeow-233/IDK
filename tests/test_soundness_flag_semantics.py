from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from clang_driver_support import cc1_changes, clang, minimal_source, option_spellings

from leanc.foundation.errors import SpecError
from leanc.rules.stack_checks import codegen_flags
from leanc.specs.compile_options import (
    CONST_STRING_MESSAGE,
    LEXICAL_MESSAGE,
    MACRO_DEFINING_WARNING_FLAGS,
    PREPROCESSOR_OUTPUT_MESSAGE,
    TOOL_CONTROL_MESSAGE,
    flag_rejection,
    is_diagnostic_option,
)
from leanc.specs.loader import parse_spec_text

REPO = Path(__file__).resolve().parent.parent
BASE_SPEC = (REPO / "examples/safe_array_get/spec.yaml").read_text()
WARNING_PREFIX = "-W"
NEGATED_WARNING_PREFIX = "-Wno-"
VALUE_PLACEHOLDER = "1"
JOINED_VALUE_SUFFIX = "="
EXTRA_DIAGNOSTIC_SPELLINGS = ["-w", "--no-warnings", "-pedantic", "-pedantic-errors"]
DIAGNOSTIC_CC1_PREFIXES = ("-W", "-w", "-pedantic", "-fwarn-stack-size=")
C_SUFFIX = ".c"
CXX_SUFFIX = ".cpp"
PREPROCESSOR_OUTPUT_SPELLINGS = [
    "-P",
    "--no-line-commands",
    "-dM",
    "-dD",
    "-dI",
    "-driver-mode=cl",
    "-fuse-line-directives",
    "-fdirectives-only",
    "-frewrite-includes",
    "-fkeep-system-includes",
    "-fminimize-whitespace",
    "-C",
    "-CC",
    "-E",
    "-M",
    "-MM",
    "-traditional-cpp",
    "-fsyntax-only",
]


def _spec_with_flags(flags: list[str]) -> str:
    data = yaml.safe_load(BASE_SPEC)
    data["compile"]["flags"] = flags
    return json.dumps(data)


def _dropped_warning_spellings() -> list[str]:
    spellings = [
        spelling + VALUE_PLACEHOLDER if spelling.endswith(JOINED_VALUE_SUFFIX) else spelling
        for spelling in [*option_spellings(WARNING_PREFIX), *EXTRA_DIAGNOSTIC_SPELLINGS]
    ]
    return [spelling for spelling in spellings if is_diagnostic_option(spelling) and flag_rejection([spelling]) is None]


def _semantic_cc1_changes(source: Path, flags: list[str]) -> tuple[list[str], list[str]]:
    added, removed = cc1_changes(source, flags)
    return (
        [arg for arg in added if not arg.startswith(DIAGNOSTIC_CC1_PREFIXES)],
        [arg for arg in removed if not arg.startswith(DIAGNOSTIC_CC1_PREFIXES)],
    )


@pytest.mark.parametrize("suffix", [C_SUFFIX, CXX_SUFFIX])
def test_warning_spellings_dropped_from_the_stack_checks_change_only_diagnostics(needs_clang, tmp_path, suffix):
    source = minimal_source(tmp_path, suffix)
    dropped = _dropped_warning_spellings()
    negated = [spelling for spelling in dropped if spelling.startswith(NEGATED_WARNING_PREFIX)]
    positive = [spelling for spelling in dropped if spelling not in negated]
    assert len(positive) > 500 and len(negated) > 500
    assert _semantic_cc1_changes(source, positive) == ([], [])
    assert _semantic_cc1_changes(source, negated) == ([], [])


def test_the_warning_scan_sees_semantic_warning_options(needs_clang, tmp_path):
    assert _semantic_cc1_changes(minimal_source(tmp_path, C_SUFFIX), ["-Wwrite-strings"]) == (["-fconst-strings"], [])
    assert _semantic_cc1_changes(minimal_source(tmp_path, CXX_SUFFIX), ["-Wno-deprecated"]) == (
        [],
        ["-fdeprecated-macro"],
    )


def test_macro_defining_warning_options_reach_the_stack_checks():
    assert codegen_flags(["-Wall", *sorted(MACRO_DEFINING_WARNING_FLAGS)]) == sorted(MACRO_DEFINING_WARNING_FLAGS)


def test_write_strings_is_rejected():
    with pytest.raises(SpecError, match="const char"):
        parse_spec_text(_spec_with_flags(["-Wall", "-Wwrite-strings"]), source="spec.json")
    assert flag_rejection(["-Wwrite-strings"]).message == CONST_STRING_MESSAGE
    assert flag_rejection(["-Wno-write-strings", "--warn-write-strings"]) is None


@pytest.mark.parametrize(
    "flag", ["--driver-mode=cl", "--driver-mode=g++", "--driver-mode", "-ivfsoverlayo.yaml", "-vfsoverlayo.yaml"]
)
def test_driver_mode_and_file_system_overlays_are_rejected(flag):
    assert flag_rejection(["-Wall", flag]).message == TOOL_CONTROL_MESSAGE


@pytest.mark.parametrize("flag", PREPROCESSOR_OUTPUT_SPELLINGS)
def test_options_that_change_the_preprocessor_output_are_rejected(flag):
    with pytest.raises(SpecError, match="must not change what the preprocessor prints"):
        parse_spec_text(_spec_with_flags(["-Wall", flag]), source="spec.json")
    assert flag_rejection([flag]).message == PREPROCESSOR_OUTPUT_MESSAGE


@pytest.mark.parametrize(
    "flag",
    ["-std=c89", "-std=c90", "-std=iso9899:1990", "-std=iso9899:199409", "--std=c89", "-ansi", "--ansi",
     "-std=c23", "-std=c2x", "-std=gnu23", "--std=gnu2x", "-fdigraphs", "-fno-digraphs"],
)
def test_options_that_change_tokenisation_are_rejected(flag):
    with pytest.raises(SpecError, match="must not change how C source is split into tokens"):
        parse_spec_text(_spec_with_flags(["-Wall", flag]), source="spec.json")
    assert flag_rejection([flag]).message == LEXICAL_MESSAGE


@pytest.mark.parametrize("flag", ["-std=gnu89", "-std=gnu90", "-std=c99", "-std=gnu17", "--std=c11"])
def test_standards_with_line_comments_and_digraphs_pass(flag):
    assert flag_rejection(["-Wall", flag]) is None


@pytest.mark.parametrize("standard", ["c89", "c90", "iso9899:1990", "iso9899:199409"])
def test_rejected_standards_are_the_ones_where_clang_reads_slash_star_as_a_division(needs_clang, tmp_path, standard):
    source = tmp_path / "slash_star.c"
    source.write_text("int x = 1 //* c */ 2\n;\n")
    preprocessed = subprocess.run([clang(), f"-std={standard}", "-E", str(source)], capture_output=True, text=True,
                                  check=True).stdout
    assert "int x = 1 / 2" in preprocessed
