from __future__ import annotations

import pytest
from integrity_support import (
    EXAMPLES,
    READ_LINE,
    by_rule,
    integrity_gate,
    shipped,
    with_flags,
)

from leanc.backends import stack_check
from leanc.foundation.status import Outcome
from leanc.lang.c_lexer import CSource
from leanc.rules.c_rules import token_findings
from leanc.rules.stack_checks import (
    UNBOUNDED_STACK_RULE,
    project_relative,
    stack_usage_findings,
    system_includes_as_user_includes,
    unbounded_stack_scan,
)
from leanc.toolchain.context import VerifyOptions

HEADER = "include/safe_array.h"
DIAG_HEADER = "include/diag.h"
SHIPPED_HEADER = (EXAMPLES / "safe_array_get" / HEADER).read_text()
HEADER_GUARD_END = "\n#endif"
UNUSED_HEADER_VLA = (
    "static inline int safe_array_scratch(size_t n) { int scratch[n + 1]; scratch[0] = 0; return scratch[0]; }\n"
)
VLA_DIAGNOSTICS_OFF = '#pragma clang diagnostic ignored "-Wvla"\n#pragma clang diagnostic ignored "-Walloca"\n'
VLA_READ = "    int scratch[len + 1];\n    scratch[0] = arr[idx];\n    *out = scratch[0];"
MS_ALLOCA_READ = "    int *scratch = _alloca((len + 1) * sizeof *scratch);\n    scratch[0] = arr[idx];\n    *out = scratch[0];"
PROMOTING_PRAGMAS = (
    '#pragma clang diagnostic error "-Wmissing-prototypes"\n#pragma clang diagnostic error "-Wreserved-identifier"\n'
)
SYSTEM_HEADER_FLAGS = [
    ["-isysteminclude"],
    ["-isystem-afterinclude"],
    ["-idirafterinclude"],
    ["--include-directory-after=include"],
    ["--system-header-prefix=safe_array.h"],
    ["-iwithsysrootinclude"],
    ["-isysteminclude", "-fdiagnostics-absolute-paths"],
    ["-isystem/proc/self/cwd/include"],
]
PRESUMED_SYSTEM_FILE = '#line 1 "/usr/include/leanc_vendor_scratch.h"\n'
INCLUDE_REWRITES = [
    (["-isystemvendor", "-isystem-aftervendor", "-idiraftervendor"], [], ["-Ivendor", "-Ivendor", "-Ivendor"]),
    (["-iwithsysrootvendor", "-iwithprefixvendor", "-cxx-isystemvendor"], [], ["-Ivendor", "-Ivendor", "-Ivendor"]),
    (["-iprefixext/", "-iwithprefixvendor", "--include-with-prefix-after=lib"], ["-iprefixext/"],
     ["-Iext/vendor", "-Iext/lib"]),
    (["--include-directory-after=vendor", "--include-with-prefix=vendor"], [], ["-Ivendor", "-Ivendor"]),
    (["-iframeworkvendor", "-iframeworkwithsysrootvendor"], [], ["-Fvendor", "-Fvendor"]),
    (["--system-header-prefix=vendor/", "-O2"], ["-O2"], []),
    (["-iwithprefixbeforevendor", "--include-with-prefix-before=vendor", "-Ivendor"],
     ["-iwithprefixbeforevendor", "--include-with-prefix-before=vendor", "-Ivendor"], []),
]
PROJECT_ROOT = "/run/snapshot"
FORCED_INCLUDE_FLAGS = [f"-include{DIAG_HEADER}", f"-imacros{DIAG_HEADER}"]
HEADERS = f"headers: [{HEADER}]"
RUNTIME_STACK = "allocates stack space at run time"
DYNAMIC_ROWS = "src/a.c:3:grows\t48\tdynamic\nsrc/a.c:7:bounded\t32\tdynamic,bounded\nsrc/a.c:9:fixed\t64\tstatic\n"
SYSTEM_HEADER_ALLOCA = (
    "/usr/include/c++/13/ext/string_conversions.h:107:38: error: use of function '__builtin_alloca' is discouraged "
    "[-Werror,-Walloca]\n"
)
UNRELATED_ERROR = "src/a.c:3:4: error: unknown type name 'foo'\n"
PEDANTIC_AS_ERROR = '#pragma clang diagnostic error "-Wpedantic"\n'
ERROR_LIMIT_FATAL = "fatal error: too many errors emitted, stopping now [-ferror-limit=]\n"
UNSANDBOXED = VerifyOptions(allow_unsandboxed=True, sandbox_preference="none")
CPP_EXAMPLE = "cpp_ring_buffer"
CPP_EDITABLE = "editable: [src/ring_buffer.cpp]"
CPP_STD_FILE = "src/std_headers.cpp"
CPP_STD_HEADERS = ["string", "bitset", "vector", "map"]
CPP_STD_SOURCE = "".join(f"#include <{name}>\n" for name in CPP_STD_HEADERS) + "inline int unused_inline() { return 1; }\n"


def _with_header(text: str) -> dict[str, str]:
    return {HEADER: SHIPPED_HEADER.replace(HEADER_GUARD_END, "\n" + text + HEADER_GUARD_END)}


@pytest.mark.parametrize("flags", SYSTEM_HEADER_FLAGS, ids=" ".join)
def test_system_header_flags_cannot_hide_a_vla_in_a_protected_header(example_copy, needs_clang, tmp_path, flags):
    result = integrity_gate(
        example_copy, tmp_path, spec_edits=with_flags("-Wall", *flags), extra_files=_with_header(UNUSED_HEADER_VLA)
    )
    assert result.outcome is Outcome.FAIL, result.summary
    assert [d.location.file for d in by_rule(result, UNBOUNDED_STACK_RULE)] == [HEADER]


def test_presumed_system_file_name_cannot_hide_a_protected_header_vla(example_copy, needs_clang, tmp_path):
    files = _with_header(PRESUMED_SYSTEM_FILE + UNUSED_HEADER_VLA)
    result = integrity_gate(example_copy, tmp_path, spec_edits=with_flags("-Wall", "-isysteminclude"), extra_files=files)
    assert result.outcome is Outcome.FAIL, result.summary
    assert [d.location.file for d in by_rule(result, UNBOUNDED_STACK_RULE)] == ["/usr/include/leanc_vendor_scratch.h"]


@pytest.mark.parametrize("flags, kept, system_as_user", INCLUDE_REWRITES, ids=lambda flags: " ".join(flags))
def test_system_include_options_become_user_include_options(flags, kept, system_as_user):
    split = system_includes_as_user_includes(flags)
    assert (split.kept, split.system_as_user) == (kept, system_as_user)


def test_frame_check_reports_a_vla_whose_diagnostics_are_switched_off(example_copy, needs_clang, tmp_path):
    source = shipped().replace(READ_LINE, VLA_READ)
    result = integrity_gate(example_copy, tmp_path, source, extra_files=_with_header(VLA_DIAGNOSTICS_OFF))
    assert result.outcome is Outcome.FAIL, result.summary
    assert [RUNTIME_STACK in d.message for d in by_rule(result, UNBOUNDED_STACK_RULE)] == [True]


def test_ms_alloca_is_an_unbounded_stack_allocation(example_copy, needs_clang, tmp_path):
    source = shipped().replace(READ_LINE, MS_ALLOCA_READ)
    result = integrity_gate(example_copy, tmp_path, source, with_flags("-Wall", "-fms-extensions"))
    assert result.outcome is Outcome.FAIL, result.summary
    found = by_rule(result, UNBOUNDED_STACK_RULE)
    assert any(RUNTIME_STACK in d.message for d in found) and any("'_alloca'" in d.message for d in found)


def test_ms_alloca_token_is_found():
    [finding] = token_findings(CSource("void *grab(unsigned long n) { return _alloca(n); }\n"))
    assert finding.rule == UNBOUNDED_STACK_RULE


def test_non_static_stack_usage_rows_are_unbounded_stack():
    usage = stack_usage_findings("src/a.c", DYNAMIC_ROWS)
    assert [(v.rule, v.line) for v in usage.violations] == [(UNBOUNDED_STACK_RULE, 3), (UNBOUNDED_STACK_RULE, 7)]


def test_every_parsed_diagnostic_counts_and_other_errors_are_unparsed():
    scan = unbounded_stack_scan(SYSTEM_HEADER_ALLOCA, "r", project_root=PROJECT_ROOT)
    [found] = scan.violations
    assert found.file.startswith("/usr/include/") and found.line == 107 and scan.accounts_for_failure
    mixed = unbounded_stack_scan(SYSTEM_HEADER_ALLOCA + UNRELATED_ERROR, "r", project_root=PROJECT_ROOT)
    assert mixed.unparsed_errors == [UNRELATED_ERROR.strip()] and not mixed.accounts_for_failure


def test_unlocated_fatal_error_is_unparsed():
    scan = unbounded_stack_scan(SYSTEM_HEADER_ALLOCA + ERROR_LIMIT_FATAL, "r", project_root=PROJECT_ROOT)
    assert scan.unparsed_errors == [ERROR_LIMIT_FATAL.strip()] and not scan.accounts_for_failure


def test_vla_in_a_header_outside_the_project_still_counts(example_copy, needs_clang, tmp_path):
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "vendor_scratch.h").write_text(UNUSED_HEADER_VLA.replace("size_t", "unsigned long"))
    source = "#include <vendor_scratch.h>\n" + shipped()
    result = integrity_gate(example_copy, tmp_path, source, with_flags("-Wall", f"-I{vendor}"), options=UNSANDBOXED)
    assert result.outcome is Outcome.FAIL, result.summary
    assert [d.location.file for d in by_rule(result, UNBOUNDED_STACK_RULE)] == [str(vendor / "vendor_scratch.h")]


@pytest.mark.parametrize("flag", FORCED_INCLUDE_FLAGS)
def test_forced_include_pragmas_cannot_break_the_stack_usage_anchor(example_copy, needs_clang, tmp_path, flag):
    edits = {**with_flags("-Wall", flag), HEADERS: f"headers: [{HEADER}, {DIAG_HEADER}]"}
    result = integrity_gate(example_copy, tmp_path, spec_edits=edits, extra_files={DIAG_HEADER: PROMOTING_PRAGMAS})
    assert result.outcome is Outcome.PASS, result.summary
    argv = result.details["files"][0]["argv"]["stack_frames"]
    assert argv.index(stack_check.INCLUDE_FLAG) < argv.index(flag)


def test_embedded_bitcode_still_writes_stack_usage(example_copy, needs_clang, tmp_path):
    result = integrity_gate(example_copy, tmp_path, spec_edits=with_flags("-Wall", "-fembed-bitcode"))
    assert result.outcome is Outcome.PASS, result.summary
    argv = result.details["files"][0]["argv"]["stack_frames"]
    assert argv.index("-fembed-bitcode=off") > argv.index("-fembed-bitcode")


def test_failing_frame_compile_reports_its_exit_code(example_copy, needs_clang, tmp_path, monkeypatch):
    monkeypatch.setattr(stack_check, "ANCHOR_SOURCE", "#error broken anchor\n")
    result = integrity_gate(example_copy, tmp_path)
    assert result.outcome is Outcome.ERROR, result.summary
    assert "stack-frame check of src/safe_array.c failed (exit 1)" in result.summary
    assert stack_check.NO_STACK_USAGE not in result.summary


def test_alloca_inside_library_headers_does_not_fail_the_vla_check(example_copy, needs_clang, tmp_path):
    edits = {CPP_EDITABLE: f"editable: [src/ring_buffer.cpp, {CPP_STD_FILE}]"}
    result = integrity_gate(
        example_copy, tmp_path, spec_edits=edits, example=CPP_EXAMPLE, extra_files={CPP_STD_FILE: CPP_STD_SOURCE}
    )
    assert result.outcome is Outcome.PASS, result.summary
    assert all("stack" in entry["checks"] for entry in result.details["files"])


@pytest.mark.parametrize(
    "file, relative",
    [
        ("include/a.h", "include/a.h"),
        ("./include/../include/a.h", "include/a.h"),
        (f"{PROJECT_ROOT}/include/a.h", "include/a.h"),
        (f"{PROJECT_ROOT}-other/include/a.h", None),
        ("/usr/include/stdio.h", None),
    ],
)
def test_project_files_are_recognised_by_location_not_spelling(file, relative):
    assert project_relative(file, PROJECT_ROOT) == relative


def test_promoting_pragma_before_library_headers_passes_the_vla_check(example_copy, needs_clang, tmp_path):
    edits = {**with_flags("-Wall", f"-include{DIAG_HEADER}"), HEADERS: f"headers: [{HEADER}, {DIAG_HEADER}]"}
    source = "#include <stdint.h>\n#include <limits.h>\n" + shipped()
    result = integrity_gate(example_copy, tmp_path, source, edits, extra_files={DIAG_HEADER: PEDANTIC_AS_ERROR})
    assert result.outcome is Outcome.PASS, result.summary


def test_error_limit_cannot_stop_the_user_include_run_before_a_protected_header(example_copy, needs_clang, tmp_path):
    flags = ["-isysteminclude", "-ferror-limit=1"]
    edits = {**with_flags("-Wall", f"-include{DIAG_HEADER}", *flags), HEADERS: f"headers: [{HEADER}, {DIAG_HEADER}]"}
    source = "#include <stdint.h>\n#include <limits.h>\n" + shipped()
    files = {**_with_header(UNUSED_HEADER_VLA), DIAG_HEADER: PEDANTIC_AS_ERROR}
    result = integrity_gate(example_copy, tmp_path, source, edits, extra_files=files)
    assert result.outcome is Outcome.FAIL, result.summary
    assert [d.location.line for d in by_rule(result, UNBOUNDED_STACK_RULE)] == [20]


def test_header_the_build_finds_first_is_checked_as_given(example_copy, needs_clang, tmp_path):
    headers = f"headers: [{HEADER}, include/scratch.h, vendor/scratch.h]"
    edits = {**with_flags("-Wall", "-isystemvendor"), HEADERS: headers}
    files = {"include/scratch.h": "#include <stddef.h>\n" + UNUSED_HEADER_VLA, "vendor/scratch.h": "#define SCRATCH 1\n"}
    source = "#include <scratch.h>\n" + shipped()
    result = integrity_gate(example_copy, tmp_path, source, edits, extra_files=files)
    assert result.outcome is Outcome.FAIL, result.summary
    assert [d.location.file for d in by_rule(result, UNBOUNDED_STACK_RULE)] == ["include/scratch.h"]
