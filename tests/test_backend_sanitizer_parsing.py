from __future__ import annotations

import textwrap

import pytest

from leanc.backends.sanitizer import (
    parse_build_diagnostics,
    parse_sanitizer_findings,
    parse_sanitizer_output,
    scan_sanitizer_control,
    ubsan_rule,
)

SNAP = "/w/run/snapshot"
PF = {"src/f.c", "include/f.h", "tests/driver.c"}

ASAN_HBO = f"""\
=================================================================
==2==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x502000000020 at pc 0x55e5f9129848 bp 0x7fff07db4cb0 sp 0x7fff07db4ca8
READ of size 4 at 0x502000000020 thread T0
    #0 0x55e5f9129847 in f {SNAP}/src/f.c:6:13
    #1 0x55e5f91298ea in main {SNAP}/tests/driver.c:4:43
    #2 0x7ff5f6c2a1c9 in __libc_start_call_main csu/../sysdeps/nptl/libc_start_call_main.h:58:16
    #3 0x7ff5f6c2a28a in __libc_start_main csu/../csu/libc-start.c:360:3
    #4 0x55e5f904f344 in _start (/w/run/build/sanitizer/test_bin+0x2c344) (BuildId: 61ee8e4fc3a458edb925d212ce4d8be7cd8e77d3)

0x502000000020 is located 0 bytes after 16-byte region [0x502000000010,0x502000000020)
allocated by thread T0 here:
    #0 0x55e5f90e9193 in malloc (/w/run/build/sanitizer/test_bin+0xc7193) (BuildId: 61ee8e4fc3a458edb925d212ce4d8be7cd8e77d3)
    #1 0x55e5f91297cc in f {SNAP}/src/f.c:3:14

SUMMARY: AddressSanitizer: heap-buffer-overflow {SNAP}/src/f.c:6:13 in f
Shadow bytes around the buggy address:
  0x501ffffffd80: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
=>0x502000000000: fa fa 00 00[fa]fa fa fa fa fa fa fa fa fa fa fa
==2==ABORTING
"""

ASAN_DOUBLE_FREE = f"""\
==2==ERROR: AddressSanitizer: attempting double-free on 0x502000000010 in thread T0:
    #0 0x5610c7291efa in free (/w/run/build/sanitizer/test_bin+0xc6efa) (BuildId: 61ee8e4fc3a458edb925d212ce4d8be7cd8e77d3)
    #1 0x5610c72d089e in f {SNAP}/src/f.c:11:28
    #2 0x5610c72d0a3a in main {SNAP}/tests/driver.c:4:43

0x502000000010 is located 0 bytes inside of 16-byte region [0x502000000010,0x502000000020)
freed by thread T0 here:
    #0 0x5610c7291efa in free (/w/run/build/sanitizer/test_bin+0xc6efa) (BuildId: 61ee8e4fc3a458edb925d212ce4d8be7cd8e77d3)
    #1 0x5610c72d0896 in f {SNAP}/src/f.c:11:19

SUMMARY: AddressSanitizer: double-free (/w/run/build/sanitizer/test_bin+0xc6efa) (BuildId: 61ee8e4fc3a458edb925d212ce4d8be7cd8e77d3) in free
==2==ABORTING
"""

ASAN_SEGV = f"""\
AddressSanitizer:DEADLYSIGNAL
=================================================================
==2==ERROR: AddressSanitizer: SEGV on unknown address 0x000000000000 (pc 0x55aec9aaa8b3 bp 0x7ffc6cde9140 sp 0x7ffc6cde90c0 T0)
==2==The signal is caused by a READ memory access.
==2==Hint: address points to the zero page.
    #0 0x55aec9aaa8b3 in f {SNAP}/src/f.c:12:57
    #1 0x55aec9aaaa3a in main {SNAP}/tests/driver.c:4:43

AddressSanitizer can not provide additional info.
SUMMARY: AddressSanitizer: SEGV {SNAP}/src/f.c:12:57 in f
==2==ABORTING
"""

ASAN_STACK = f"""\
==7==ERROR: AddressSanitizer: stack-buffer-overflow on address 0x7ffd1c2f0a48 at pc 0x55 bp 0x7ffd sp 0x7ffd
WRITE of size 4 at 0x7ffd1c2f0a48 thread T0
    #0 0x55d1 in fill {SNAP}/src/f.c:5:14
    #1 0x55d2 in f {SNAP}/src/f.c:10:5
    #2 0x55d3 in main {SNAP}/tests/driver.c:4:43

Address 0x7ffd1c2f0a48 is located in stack of thread T0 at offset 40 in frame
    #0 0x55d4 in f {SNAP}/src/f.c:8

  This frame has 1 object(s):
    [32, 40) 'buf' (line 9) <== Memory access at offset 40 overflows this variable
SUMMARY: AddressSanitizer: stack-buffer-overflow {SNAP}/src/f.c:5:14 in fill
"""

UBSAN_OVF = f"""\
src/f.c:6:16: runtime error: signed integer overflow: 2147483600 + 100 cannot be represented in type 'int'
    #0 0x55fe5604c981 in f {SNAP}/src/f.c:6:16
    #1 0x55fe5604ca3a in main {SNAP}/tests/driver.c:8:13
    #2 0x7fd45022a1c9 in __libc_start_call_main csu/../sysdeps/nptl/libc_start_call_main.h:58:16

SUMMARY: UndefinedBehaviorSanitizer: undefined-behavior src/f.c:6:16
"""

LSAN_COMBINED = f"""\
r=42

=================================================================
==2==ERROR: LeakSanitizer: detected memory leaks

Direct leak of 64 byte(s) in 1 object(s) allocated from:
    #0 0x55780ee72193 in malloc (/w/run/build/sanitizer/test_bin+0xc7193) (BuildId: 61ee8e4fc3a458edb925d212ce4d8be7cd8e77d3)
    #1 0x55780eeb07cc in f {SNAP}/src/f.c:9:17
    #2 0x55780eeb0a3a in main {SNAP}/tests/driver.c:8:13
    #3 0x7f38f822a1c9 in __libc_start_call_main csu/../sysdeps/nptl/libc_start_call_main.h:58:16

Indirect leak of 8 byte(s) in 2 object(s) allocated from:
    #0 0x55780ee72193 in malloc (/w/run/build/sanitizer/test_bin+0xc7193) (BuildId: 61ee8e4fc3a458edb925d212ce4d8be7cd8e77d3)
    #1 0x55780eeb07dd in make_node {SNAP}/src/f.c:20:9

SUMMARY: AddressSanitizer: 72 byte(s) leaked in 3 allocation(s).
"""


def test_parse_asan_heap_buffer_overflow():
    diags = parse_sanitizer_output(ASAN_HBO, project_files=PF, base_dir=SNAP)
    assert len(diags) == 1
    d = diags[0]
    assert (d.tool, d.rule_id, d.severity) == ("asan", "heap-buffer-overflow", "error")
    assert (d.location.file, d.location.line, d.location.column, d.location.function) == ("src/f.c", 6, 13, "f")
    assert "READ of size 4" in d.message and "0 bytes after 16-byte region" in d.message
    assert "0x502000000020" not in d.message
    assert d.raw.startswith("==2==ERROR: AddressSanitizer: heap-buffer-overflow")
    assert "Shadow bytes" not in d.raw and "SUMMARY" in d.raw


def test_parse_location_without_base_dir_uses_suffix_match():
    d = parse_sanitizer_output(ASAN_HBO, project_files=PF)[0]
    assert (d.location.file, d.location.line) == ("src/f.c", 6)


def test_parse_location_without_project_files_uses_first_non_system_frame():
    d = parse_sanitizer_output(ASAN_DOUBLE_FREE)[0]
    assert d.location.file == f"{SNAP}/src/f.c" and d.location.line == 11
    d = parse_sanitizer_output(ASAN_DOUBLE_FREE, base_dir=SNAP)[0]
    assert d.location.file == "src/f.c"


def test_parse_location_prefers_project_frame_over_other_files():
    text = ASAN_HBO.replace(f"in f {SNAP}/src/f.c:6:13", "in helper /home/x/other/lib.c:3:1")
    d = parse_sanitizer_output(text, project_files=PF, base_dir=SNAP)[0]
    assert d.location.file == "tests/driver.c"


def test_parse_double_free_and_segv_and_stack_overflow():
    d = parse_sanitizer_output(ASAN_DOUBLE_FREE, project_files=PF, base_dir=SNAP)[0]
    assert d.rule_id == "double-free" and (d.location.file, d.location.line) == ("src/f.c", 11)
    d = parse_sanitizer_output(ASAN_SEGV, project_files=PF, base_dir=SNAP)[0]
    assert d.rule_id == "SEGV" and (d.location.file, d.location.line) == ("src/f.c", 12)
    assert "zero page" in d.message and "READ memory access" in d.message
    d = parse_sanitizer_output(ASAN_STACK, project_files=PF, base_dir=SNAP)[0]
    assert d.rule_id == "stack-buffer-overflow"
    assert (d.location.file, d.location.line, d.location.function) == ("src/f.c", 5, "fill")
    assert "WRITE of size 4" in d.message and "'buf'" in d.message


def test_parse_header_kind_without_summary():
    text = "==3==ERROR: AddressSanitizer: heap-use-after-free on address 0x6 at pc 0x1 bp 0x2 sp 0x3\n" \
           f"READ of size 4 at 0x6 thread T0\n    #0 0x1 in f {SNAP}/src/f.c:7:32\n"
    d = parse_sanitizer_output(text, project_files=PF, base_dir=SNAP)[0]
    assert d.rule_id == "heap-use-after-free" and d.location.line == 7
    bad_free = "==3==ERROR: AddressSanitizer: attempting free on address which was not malloc()-ed: 0x7ffd in thread T0\n"
    assert parse_sanitizer_output(bad_free)[0].rule_id == "bad-free"


def test_parse_ubsan_signed_overflow():
    findings = parse_sanitizer_findings(UBSAN_OVF, project_files=PF, base_dir=SNAP)
    assert len(findings) == 1
    d = findings[0].diagnostic
    assert (d.tool, d.rule_id) == ("ubsan", "signed-integer-overflow")
    assert (d.location.file, d.location.line, d.location.column, d.location.function) == ("src/f.c", 6, 16, "f")
    assert findings[0].call_stack()[0] == "#0 f src/f.c:6:16"
    assert "SUMMARY" in d.raw


@pytest.mark.parametrize(
    "msg, rule",
    [
        ("signed integer overflow: 2147483647 + 1 cannot be represented in type 'int'", "signed-integer-overflow"),
        ("negation of -2147483648 cannot be represented in type 'int'", "signed-integer-overflow"),
        ("division of -2147483648 by -1 cannot be represented in type 'int'", "signed-integer-overflow"),
        ("division by zero", "division-by-zero"),
        ("index 4 out of bounds for type 'int[4]'", "out-of-bounds-index"),
        ("load of misaligned address 0x7ff77fc00041 for type 'int', which requires 4 byte alignment", "misaligned"),
        ("load of null pointer of type 'int'", "null-pointer"),
        ("null pointer passed as argument 1, which is declared to never be null", "null-pointer"),
        ("applying zero offset to null pointer", "pointer-overflow"),
        ("shift exponent 33 is too large for 32-bit type 'int'", "shift"),
        ("left shift of negative value -1", "shift"),
        ("load of address 0x5 with insufficient space for an object of type 'int'", "object-size"),
        ("load of value 7, which is not a valid value for type '_Bool'", "invalid-value"),
        ("something new and unexpected", "ubsan-runtime-error"),
    ],
)
def test_ubsan_rule_mapping(msg, rule):
    assert ubsan_rule(msg) == rule
    d = parse_sanitizer_output(f"src/f.c:3:9: runtime error: {msg}\n", project_files=PF)[0]
    assert d.rule_id == rule and d.location.line == 3


def test_parse_lsan_direct_and_indirect_leaks():
    diags = parse_sanitizer_output(LSAN_COMBINED, project_files=PF, base_dir=SNAP)
    assert [d.tool for d in diags] == ["lsan", "lsan"]
    assert all(d.rule_id == "memory-leak" for d in diags)
    direct, indirect = diags
    assert "Direct leak of 64 byte(s) in 1 object(s)" in direct.message
    assert (direct.location.file, direct.location.line, direct.location.function) == ("src/f.c", 9, "f")
    assert "Indirect leak of 8 byte(s)" in indirect.message and indirect.location.function == "make_node"


def test_parse_standalone_lsan_and_rss_limit():
    text = (
        "==2==ERROR: LeakSanitizer: detected memory leaks\n\n"
        "Direct leak of 16 byte(s) in 1 object(s) allocated from:\n"
        "    #0 0x5578 in malloc (/w/run/build/sanitizer/test_bin+0x33239) (BuildId: edf0)\n"
        f"    #1 0x5578 in f {SNAP}/src/f.c:4:14\n\n"
        "SUMMARY: LeakSanitizer: 16 byte(s) leaked in 1 allocation(s).\n"
    )
    d = parse_sanitizer_output(text, project_files=PF, base_dir=SNAP)
    assert len(d) == 1 and d[0].tool == "lsan" and d[0].location.line == 4
    rss = "==9==AddressSanitizer: hard rss limit exhausted (300Mb vs 315Mb)\n==9==Process memory map follows:\n\t0x1-0x2\t/usr/lib/x\n"
    d = parse_sanitizer_output(rss)
    assert len(d) == 1 and d[0].tool == "asan" and d[0].rule_id == "rss-limit-exceeded"
    assert "300Mb vs 315Mb" in d[0].message and "memory map" not in d[0].raw


def test_parse_summary_only_and_clean_output():
    assert parse_sanitizer_output("r=42\nall good\n") == []
    assert parse_sanitizer_output("") == []
    d = parse_sanitizer_output("SUMMARY: UndefinedBehaviorSanitizer: undefined-behavior src/f.c:8:42\n",
                               project_files=PF)
    assert len(d) == 1 and d[0].tool == "ubsan" and d[0].rule_id == "ubsan-runtime-error"
    assert (d[0].location.file, d[0].location.line) == ("src/f.c", 8)


def test_parse_multiple_reports_in_one_stream():
    text = UBSAN_OVF + "src/f.c:9:3: runtime error: division by zero\n" + ASAN_HBO
    diags = parse_sanitizer_output(text, project_files=PF, base_dir=SNAP)
    assert [(d.tool, d.rule_id) for d in diags] == [
        ("ubsan", "signed-integer-overflow"),
        ("ubsan", "division-by-zero"),
        ("asan", "heap-buffer-overflow"),
    ]


def test_parse_build_diagnostics_linker_and_compiler():
    text = (
        "src/f.c:3:12: warning: unused variable 'x' [-Wunused-variable]\n"
        "/usr/bin/ld: /tmp/f-26a909.o: in function `f':\n"
        f"{SNAP}/src/f.c:6:(.text+0xe6): undefined reference to `helper'\n"
        "clang: error: linker command failed with exit code 1 (use -v to see invocation)\n"
    )
    diags = parse_build_diagnostics(text, base_dir=SNAP)
    assert [d.rule_id for d in diags] == ["-Wunused-variable", "undefined-reference", "link-error"]
    ref = diags[1]
    assert (ref.location.file, ref.location.line, ref.location.function) == ("src/f.c", 6, "f")
    assert ref.message == "undefined reference to `helper'"


def test_scan_sanitizer_control(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.c").write_text(
        textwrap.dedent(
            """\
            // __lsan_is_turned_off in a comment is fine
            const char *s = "/* not a comment";
            int __lsan_is_turned_off(void) { return 1; }
            __attribute__((no_sanitize("address"))) int g(int *p) { return *p; }
            #include <sanitizer/asan_interface.h>
            #if __has_feature(address_sanitizer)
            #endif
            /* __asan_default_options */
            """
        )
    )
    diags = scan_sanitizer_control(tmp_path, ["src/a.c"])
    assert sorted(d.location.line for d in diags) == [3, 4, 5, 6]
    assert all(d.tool == "leanc-policy" and d.rule_id == "sanitizer-control" for d in diags)


def test_parse_frame_without_line_info():
    text = (
        "src/sensor.c:14:13: runtime error: member access within null pointer of type 'const struct sensor'\n"
        f"    #0 0x562511d41880 in sensor_read {SNAP}/src/sensor.c\n"
        f"    #1 0x562511d41c04 in main {SNAP}/tests/driver.c:42:16\n"
        "SUMMARY: UndefinedBehaviorSanitizer: undefined-behavior src/sensor.c:14:13\n"
    )
    f = parse_sanitizer_findings(text, project_files={"src/sensor.c", "tests/driver.c"}, base_dir=SNAP)[0]
    d = f.diagnostic
    assert (d.rule_id, d.location.file, d.location.line, d.location.function) == (
        "null-pointer", "src/sensor.c", 14, "sensor_read"
    )
    assert f.call_stack()[:2] == ["#0 sensor_read src/sensor.c", "#1 main tests/driver.c:42:16"]
    asan = "==1==ERROR: AddressSanitizer: SEGV on unknown address 0x000000000008\n" \
           f"    #0 0x1 in sensor_read {SNAP}/src/sensor.c\n"
    d = parse_sanitizer_output(asan, project_files={"src/sensor.c"}, base_dir=SNAP)[0]
    assert (d.location.file, d.location.line, d.location.function) == ("src/sensor.c", None, "sensor_read")


def test_parse_ubsan_without_location_and_no_false_positive():
    d = parse_sanitizer_output("<unknown>: runtime error: division by zero\n")
    assert len(d) == 1 and d[0].rule_id == "division-by-zero" and d[0].location is None
    d = parse_sanitizer_output("(/w/bin/test_bin+0x1234): runtime error: load of null pointer of type 'int'\n")
    assert len(d) == 1 and d[0].rule_id == "null-pointer" and d[0].location is None
    assert parse_sanitizer_output("mytool: runtime error: config file missing\n") == []
