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
    default_system_dirs,
    project_relative,
    stack_usage_findings,
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
    ["-idirafterinclude"],
    ["--system-header-prefix=safe_array.h"],
    ["-iwithsysrootinclude"],
    ["-isysteminclude", "-fdiagnostics-absolute-paths"],
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
SYSTEM_HEADER_EXTENSION_ERROR = (
    "/usr/lib/llvm-18/lib/clang/18/include/stdint.h:52:3: error: #include_next is a language extension "
    "[-Werror,-Wgnu-include-next]\n"
)
SYSTEM_HEADER_FATAL_ERROR = "/usr/include/stdio.h:27:10: fatal error: 'bits/libc-header-start.h' file not found\n"
PEDANTIC_AS_ERROR = '#pragma clang diagnostic error "-Wpedantic"\n'
SYSTEM_DIRS = ("/usr/lib/llvm-18/lib/clang/18/include", "/usr/include")
ALIASED_HEADER_VLA = "/proc/self/cwd/include/safe_array.h:21:71: error: variable length array used [-Werror,-Wvla]\n"
ERROR_LIMIT_FATAL = "fatal error: too many errors emitted, stopping now [-ferror-limit=]\n"
SEARCH_LIST = (
    '#include "..." search starts here:\n#include <...> search starts here:\n'
    " /usr/bin/../lib/gcc/x86_64-linux-gnu/13/../../../../include/c++/13\n /usr/include\nEnd of search list.\n"
)
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


def test_system_header_scope_ignores_only_parsed_diagnostics_in_system_dirs():
    outside = unbounded_stack_scan(SYSTEM_HEADER_ALLOCA, "r", project_root=PROJECT_ROOT, tolerated_dirs=SYSTEM_DIRS)
    assert outside.violations == [] and outside.accounts_for_failure
    mixed = unbounded_stack_scan(
        SYSTEM_HEADER_ALLOCA + UNRELATED_ERROR, "r", project_root=PROJECT_ROOT, tolerated_dirs=SYSTEM_DIRS
    )
    assert mixed.unparsed_errors == [UNRELATED_ERROR.strip()] and not mixed.accounts_for_failure


def test_paths_outside_the_system_dirs_count_even_when_absolute():
    [found] = unbounded_stack_scan(ALIASED_HEADER_VLA, "r", project_root=PROJECT_ROOT, tolerated_dirs=SYSTEM_DIRS).violations
    assert found.file == "/proc/self/cwd/include/safe_array.h" and found.line == 21


def test_unlocated_fatal_error_is_unparsed():
    scan = unbounded_stack_scan(
        SYSTEM_HEADER_EXTENSION_ERROR + ERROR_LIMIT_FATAL, "r", project_root=PROJECT_ROOT, tolerated_dirs=SYSTEM_DIRS
    )
    assert scan.unparsed_errors == [ERROR_LIMIT_FATAL.strip()] and not scan.accounts_for_failure


def test_default_scope_counts_every_reported_file():
    scan = unbounded_stack_scan(SYSTEM_HEADER_ALLOCA, "r", project_root=PROJECT_ROOT, tolerated_dirs=())
    [found] = scan.violations
    assert found.file.startswith("/usr/include/") and found.line == 107


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


def test_project_scope_tolerates_recoverable_errors_in_library_headers():
    recoverable = unbounded_stack_scan(SYSTEM_HEADER_EXTENSION_ERROR, "r", project_root=PROJECT_ROOT,
                                       tolerated_dirs=SYSTEM_DIRS)
    assert recoverable.unparsed_errors == [] and recoverable.accounts_for_failure
    fatal = unbounded_stack_scan(SYSTEM_HEADER_FATAL_ERROR, "r", project_root=PROJECT_ROOT, tolerated_dirs=SYSTEM_DIRS)
    assert not fatal.accounts_for_failure
    default = unbounded_stack_scan(SYSTEM_HEADER_EXTENSION_ERROR, "r", project_root=PROJECT_ROOT, tolerated_dirs=())
    assert not default.accounts_for_failure


def test_promoting_pragma_before_library_headers_passes_the_vla_check(example_copy, needs_clang, tmp_path):
    edits = {**with_flags("-Wall", f"-include{DIAG_HEADER}"), HEADERS: f"headers: [{HEADER}, {DIAG_HEADER}]"}
    source = "#include <stdint.h>\n#include <limits.h>\n" + shipped()
    result = integrity_gate(example_copy, tmp_path, source, edits, extra_files={DIAG_HEADER: PEDANTIC_AS_ERROR})
    assert result.outcome is Outcome.PASS, result.summary


def test_default_system_dirs_are_read_from_the_search_list():
    assert default_system_dirs(SEARCH_LIST) == ("/usr/include/c++/13", "/usr/include")
    assert default_system_dirs("clang version 18\n") is None
    assert default_system_dirs("#include <...> search starts here:\n /usr/include\n") is None


@pytest.mark.parametrize("flags", [["-isystem/proc/self/cwd/include"], ["-isysteminclude", "-ferror-limit=1"]],
                         ids=" ".join)
def test_aliased_or_truncated_system_header_runs_still_report_a_protected_header_vla(
    example_copy, needs_clang, tmp_path, flags
):
    edits = {**with_flags("-Wall", f"-include{DIAG_HEADER}", *flags), HEADERS: f"headers: [{HEADER}, {DIAG_HEADER}]"}
    source = "#include <stdint.h>\n#include <limits.h>\n" + shipped()
    files = {**_with_header(UNUSED_HEADER_VLA), DIAG_HEADER: PEDANTIC_AS_ERROR}
    result = integrity_gate(example_copy, tmp_path, source, edits, extra_files=files)
    assert result.outcome is Outcome.FAIL, result.summary
    assert [d.location.line for d in by_rule(result, UNBOUNDED_STACK_RULE)] == [20]


def test_unreadable_search_list_is_an_error(example_copy, needs_clang, tmp_path, monkeypatch):
    monkeypatch.setattr(stack_check, "SEARCH_LIST_PROBE_FLAGS", ("-E",))
    result = integrity_gate(example_copy, tmp_path)
    assert result.outcome is Outcome.ERROR and stack_check.UNREADABLE_SEARCH_LIST in result.summary, result.summary


def test_user_header_inside_a_default_system_dir_still_counts(example_copy, needs_clang, tmp_path, monkeypatch):
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "vendor_scratch.h").write_text(UNUSED_HEADER_VLA.replace("size_t", "unsigned long"))
    probed = stack_check.default_system_dirs
    monkeypatch.setattr(stack_check, "default_system_dirs", lambda stderr: (*probed(stderr), str(vendor)))
    source = "#include <vendor_scratch.h>\n" + shipped()
    result = integrity_gate(example_copy, tmp_path, source, with_flags("-Wall", f"-I{vendor}"), options=UNSANDBOXED)
    assert result.outcome is Outcome.FAIL, result.summary
    assert [d.location.file for d in by_rule(result, UNBOUNDED_STACK_RULE)] == [str(vendor / "vendor_scratch.h")]
