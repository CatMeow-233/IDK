from __future__ import annotations

from integrity_support import EXAMPLES, by_rule, integrity_gate, shipped, with_flags

from leanc.foundation.status import Outcome
from leanc.rules.stack_checks import UNBOUNDED_STACK_RULE

HEADER = "include/safe_array.h"
SHIPPED_HEADER = (EXAMPLES / "safe_array_get" / HEADER).read_text()
STDDEF_INCLUDE = "#include <stddef.h>\n"
HEADERS = f"headers: [{HEADER}]"
SCRATCH = "safe_array_scratch.h"
UNUSED_VLA = "static inline int safe_array_scratch(size_t n) { int scratch[n + 1]; scratch[0] = 0; return scratch[0]; }\n"
UNUSED_PLAIN = "static inline int safe_array_scratch(size_t n) { (void)n; return 0; }\n"
DEFINE_ENABLED_VLA = "#ifdef SCRATCH_VLA\n" + UNUSED_VLA + "#endif\n"
PROJECT_OVERRIDE = (
    "#ifndef SCRATCH_OVERRIDE_H\n#define SCRATCH_OVERRIDE_H\n#include <stddef.h>\n#define SCRATCH_VIA_PROJECT 1\n"
    f"#if __has_include_next(<{SCRATCH}>)\n#include_next <{SCRATCH}>\n#else\n{UNUSED_PLAIN}#endif\n#endif\n"
)
VENDOR_SCRATCH = (
    "#ifndef SCRATCH_VENDOR_H\n#define SCRATCH_VENDOR_H\n#include <stddef.h>\n#ifdef SCRATCH_VIA_PROJECT\n"
    f"{UNUSED_VLA}#else\n{UNUSED_PLAIN}#endif\n#endif\n"
)
SYSTEM_MODULE_MAP = f'module safe_array_scratch [system] {{\n  header "{SCRATCH}"\n  export *\n}}\n'
DIAGNOSTICS_OFF = '#pragma clang diagnostic ignored "-Wvla"\n#pragma clang diagnostic ignored "-Walloca"\n'
EDITABLE_UNUSED_VLA = "static int safe_array_scratch(size_t n)\n{\n    volatile int scratch[n + 1];\n    scratch[0] = 0;\n    return scratch[0];\n}\n"
VENDOR_CONFIG = "#define VENDOR_CONFIG 1\n"


def _header_including(text: str) -> str:
    return SHIPPED_HEADER.replace(STDDEF_INCLUDE, STDDEF_INCLUDE + text, 1)


def _headers(*extra: str) -> dict[str, str]:
    return {HEADERS: f"headers: [{', '.join([HEADER, *extra])}]"}


def _vla_files(result) -> list[str | None]:
    return [d.location.file for d in by_rule(result, UNBOUNDED_STACK_RULE)]


def test_header_vla_enabled_by_a_flags_define_is_seen_under_isystem(example_copy, needs_clang, tmp_path):
    files = {HEADER: _header_including(DEFINE_ENABLED_VLA)}
    edits = with_flags("-Wall", "-isysteminclude", "-DSCRATCH_VLA")
    result = integrity_gate(example_copy, tmp_path, spec_edits=edits, extra_files=files)
    assert result.outcome is Outcome.FAIL, result.summary
    assert _vla_files(result) == [HEADER]


def test_rewritten_system_dirs_keep_the_build_search_order(example_copy, needs_clang, tmp_path):
    files = {
        HEADER: _header_including(f"#include <{SCRATCH}>\n"),
        f"include/{SCRATCH}": PROJECT_OVERRIDE,
        f"vendor/{SCRATCH}": VENDOR_SCRATCH,
    }
    edits = {**with_flags("-Wall", "-isystemvendor"), **_headers(f"include/{SCRATCH}", f"vendor/{SCRATCH}")}
    result = integrity_gate(example_copy, tmp_path, spec_edits=edits, extra_files=files)
    assert result.outcome is Outcome.FAIL, result.summary
    assert _vla_files(result) == [f"vendor/{SCRATCH}"]


def test_system_module_map_cannot_hide_a_header_vla(example_copy, needs_clang, tmp_path):
    files = {
        HEADER: _header_including(f'#include "{SCRATCH}"\n'),
        f"include/{SCRATCH}": STDDEF_INCLUDE + UNUSED_VLA,
        "include/module.modulemap": SYSTEM_MODULE_MAP,
    }
    edits = {**with_flags("-Wall", "-fmodules"), **_headers(f"include/{SCRATCH}", "include/module.modulemap")}
    result = integrity_gate(example_copy, tmp_path, spec_edits=edits, extra_files=files)
    assert result.outcome is Outcome.FAIL, result.summary
    assert f"include/{SCRATCH}" in _vla_files(result)


def test_forced_include_pragma_cannot_hide_an_editable_vla(example_copy, needs_clang, tmp_path):
    source = shipped().replace('#include "safe_array.h"\n', '#include "safe_array.h"\n' + EDITABLE_UNUSED_VLA, 1)
    edits = {**with_flags("-Wall", "-includeinclude/build.h"), **_headers("include/build.h")}
    result = integrity_gate(example_copy, tmp_path, source, edits, extra_files={"include/build.h": DIAGNOSTICS_OFF})
    assert result.outcome is Outcome.FAIL, result.summary
    assert "src/safe_array.c" in _vla_files(result)


def test_views_that_do_not_compile_add_nothing(example_copy, needs_clang, tmp_path):
    files = {HEADER: _header_including("#include <vendor_config.h>\n"), "vendor/vendor_config.h": VENDOR_CONFIG}
    edits = {**with_flags("-Wall", "-Ivendor"), **_headers("vendor/vendor_config.h")}
    result = integrity_gate(example_copy, tmp_path, spec_edits=edits, extra_files=files)
    assert result.outcome is Outcome.PASS, result.summary
