"""Repair / generation policy: every rule, count-based "new construct" semantics, no false positives from literals."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanc.lang.c_lexer import CSource, strip_c
from leanc.repair.patch import proposal_to_contents
from leanc.repair.policy import RULES, check_candidate, check_generated
from leanc.rules.definitions import find_function_definitions, is_trivial_body
from leanc.rules.source import source_violations
from leanc.specs.loader import load_spec
from leanc.specs.model import Spec

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "examples/safe_array_get"
GOOD = (EXAMPLE / "src/safe_array.c").read_text()
BUGGY = GOOD.replace("if (idx >= len) {", "if (idx > len) {")
SRC = "src/safe_array.c"


@pytest.fixture(scope="module")
def spec() -> Spec:
    return load_spec(EXAMPLE / "spec.yaml")


@pytest.fixture(scope="module")
def spec2(spec) -> Spec:
    """Same spec with two editable files."""
    data = spec.model_dump(mode="json")
    data["sources"]["editable"] = ["src/safe_array.c", "src/helper.c"]
    return Spec.model_validate(data)


def rules(decision) -> set[str]:
    return {v.rule for v in decision.violations}


def cand(spec, new_src: str, old_src: str = BUGGY, **kw):
    return check_candidate(spec, {SRC: old_src}, {SRC: new_src}, **kw)


def inject(code: str, before: str = "    if (idx > len) {") -> str:
    """Insert ``code`` (one or more lines) into the buggy function body."""
    assert before in BUGGY
    return BUGGY.replace(before, code + "\n" + before)


def at_top(code: str) -> str:
    return BUGGY.replace('#include "safe_array.h"\n', '#include "safe_array.h"\n' + code + "\n")


def test_strip_c_blanks_comments_and_literals_preserving_lines():
    text = 'int a; /* __CPROVER_assume(x)\n still comment */ int b; // system("x")\nchar *s = "asm(\\"q\\")"; char c = \'"\';\n'
    code = strip_c(text)
    assert len(code) == len(text) and code.count("\n") == text.count("\n")
    assert "__CPROVER" not in code and "system" not in code and "asm" not in code
    assert "int a;" in code and "int b;" in code and 'char *s = "' in code and "char c = ' '" in code


def test_lexer_line_splicing_digraphs_and_line_numbers():
    text = "int x;\n__CPRO\\\nVER_assume(1);\n%:include \"leanc.h\"\nvoid f(void) <% %>\n"
    src = CSource(text)
    assert "__CPROVER_assume" in src.code
    assert "# include" in src.code and "{ " in src.code and "} " in src.code
    off = src.code.index("__CPROVER_assume")
    assert src.line_of(off) == 2
    assert src.line_of(src.code.index("include")) == 4


def test_lexer_splice_with_trailing_blanks_like_clang():
    assert "__CPROVER_x" in CSource("int __CPRO\\  \nVER_x;\n").code


def test_lexer_line_comment_continues_after_backslash():
    code = strip_c("// comment \\\n__CPROVER_assume(1);\nint ok;\n")
    assert "__CPROVER" not in code and "int ok;" in code


def test_lexer_unterminated_quote_only_hides_its_own_line():
    code = strip_c("#error don't do this\n__CPROVER_assume(1);\n")
    assert "__CPROVER_assume" in code


def test_lexer_blanks_if0_regions():
    text = "#if 0\nsystem(\"x\");\n#else\nint live1;\n#endif\n#if 1\nint live2;\n#else\nint dead2;\n#endif\n"
    code = strip_c(text)
    assert "system" not in code and "dead2" not in code
    assert "live1" in code and "live2" in code
    nested = strip_c("#if 0\n#ifdef X\nint a;\n#else\nint b;\n#endif\n#endif\nint c;\n")
    assert "int a" not in nested and "int b" not in nested and "int c" in nested
    other = strip_c("#ifdef FOO\nint a;\n#else\nint b;\n#endif\n")
    assert "int a" in other and "int b" in other, "unknown conditions are treated as live"
    chain = strip_c("#if 0\nint a;\n#elif 1\nint b;\n#else\nint c;\n#endif\nint d;\n")
    assert "int a" not in chain and "int b" in chain and "int c" not in chain and "int d" in chain
    chain2 = strip_c("#if X\nint a;\n#elif 0\nint b;\n#else\nint c;\n#endif\n")
    assert "int a" in chain2 and "int b" not in chain2 and "int c" in chain2
    inner = strip_c("#if 0\n#if 1\nint a;\n#else\nint b;\n#endif\nint c;\n#endif\nint d;\n")
    assert "int a" not in inner and "int b" not in inner and "int c" not in inner and "int d" in inner


@pytest.mark.parametrize(
    "junk",
    [
        "__attribute__((" * 20000,
        "__attribute__((" * 10000 + "))" * 10000,
        "[[" * 50000,
        "__pragma(" * 10000 + ")" * 10000,
        "safe_array_get(" * 20000,
        "int safe_array_get(){" * 20000,
        "#if X\n" * 50000,
        "asm " * 50000,
    ],
    ids=["attr", "attr-nested", "std-attr", "ms-pragma", "calls", "defs", "ifs", "one-line"],
)
def test_hostile_input_is_checked_in_linear_time(spec, junk):
    import time

    t = time.monotonic()
    check_generated(spec, {SRC: GOOD + junk})
    assert time.monotonic() - t < 5.0


def test_real_fix_is_allowed(spec):
    d = cand(spec, GOOD)
    assert d.allowed, d.violations
    assert check_generated(spec, {SRC: GOOD}).allowed


def test_rule_catalogue_is_documented():
    for r in (
        "PATH_INVALID", "PATH_NOT_EDITABLE", "NO_CHANGE", "TOO_LARGE", "CPROVER_BUILTIN",
        "ASSUMPTION_INJECTION", "ASSERT_REMOVED", "WARNING_SUPPRESSION", "SANITIZER_SUPPRESSION",
        "INCLUDE_ESCAPE", "DANGEROUS_CALL", "INLINE_ASM", "TRIVIALIZED_TARGET", "MISSING_TARGET",
    ):
        assert r in RULES


def test_no_change(spec):
    d = cand(spec, BUGGY)
    assert not d.allowed and rules(d) == {"NO_CHANGE"}


@pytest.mark.parametrize(
    "path, rule",
    [
        ("/etc/passwd", "PATH_INVALID"),
        ("../outside.c", "PATH_INVALID"),
        ("src/../../x.c", "PATH_INVALID"),
        ("src\\safe_array.c", "PATH_INVALID"),
        ("./src/safe_array.c", "PATH_INVALID"),
        ("harness/safe_array_get_harness.c", "PATH_NOT_EDITABLE"),
        ("tests/test_safe_array_get.c", "PATH_NOT_EDITABLE"),
        ("include/safe_array.h", "PATH_NOT_EDITABLE"),
        ("spec.yaml", "PATH_NOT_EDITABLE"),
        (".leanc/approvals/safe-array-get.json", "PATH_NOT_EDITABLE"),
        ("src/other.c", "PATH_NOT_EDITABLE"),
        ("Makefile", "PATH_NOT_EDITABLE"),
    ],
)
def test_path_rules(spec, path, rule):
    d = check_candidate(spec, {SRC: BUGGY}, {SRC: GOOD, path: "int x;\n"})
    assert not d.allowed
    assert [v.rule for v in d.violations if v.file == path] == [rule]


def test_path_not_editable_messages_name_the_protected_role(spec):
    d = check_candidate(
        spec,
        {SRC: BUGGY},
        {SRC: BUGGY, "harness/safe_array_get_harness.c": "x", "tests/test_safe_array_get.c": "y", "include/safe_array.h": "z"},
    )
    msgs = {v.file: v.message for v in d.violations}
    assert "harness" in msgs["harness/safe_array_get_harness.c"]
    assert "test driver" in msgs["tests/test_safe_array_get.c"]
    assert "header" in msgs["include/safe_array.h"]


def test_file_deleted(spec2):
    d = check_candidate(spec2, {SRC: GOOD, "src/helper.c": "int h;\n"}, {SRC: GOOD})
    assert rules(d) == {"FILE_DELETED"}


def test_dev_null_new_file_allowed_only_if_editable(spec2):
    current = {SRC: BUGGY}
    create = "--- /dev/null\n+++ b/src/helper.c\n@@ -0,0 +1,2 @@\n+#include \"safe_array.h\"\n+int helper_value = 3;\n"
    candidate = proposal_to_contents(None, create, current)
    assert candidate["src/helper.c"].startswith("#include")
    assert check_candidate(spec2, current, candidate).allowed
    other = create.replace("src/helper.c", "src/other.c")
    d = check_candidate(spec2, current, proposal_to_contents(None, other, current))
    assert rules(d) == {"PATH_NOT_EDITABLE"}
    harness = create.replace("src/helper.c", "harness/new_harness.c")
    d = check_candidate(spec2, current, proposal_to_contents(None, harness, current))
    assert rules(d) == {"PATH_NOT_EDITABLE"}


def test_full_file_proposal_for_harness_is_path_not_editable(spec):
    current = {SRC: BUGGY}
    candidate = proposal_to_contents({"harness/safe_array_get_harness.c": "/* weakened */\n"}, None, current)
    assert rules(check_candidate(spec, current, candidate)) == {"PATH_NOT_EDITABLE"}
    candidate = proposal_to_contents({"/abs/x.c": "int x;\n"}, None, current)
    assert rules(check_candidate(spec, current, candidate)) == {"PATH_INVALID"}


def test_too_large_default_and_explicit(spec):
    over_the_spec_default_of_200 = GOOD + "".join(f"static int pad{i} = {i};\n" for i in range(250))
    assert "TOO_LARGE" in rules(cand(spec, over_the_spec_default_of_200))
    assert "TOO_LARGE" not in rules(cand(spec, over_the_spec_default_of_200, max_changed_lines=1000))
    d = cand(spec, GOOD, max_changed_lines=1)
    assert rules(d) == {"TOO_LARGE"} and "+1 -1" in d.violations[0].message
    assert cand(spec, GOOD, max_changed_lines=2).allowed


FILE_SCOPE_PREFIX = "TOP:"
CASES: list[tuple[str, str, str]] = [
    ("CPROVER_BUILTIN", "    __CPROVER_assume(idx < len);", "plain __CPROVER_assume"),
    ("CPROVER_BUILTIN", "    __CPROVER_assert(idx < len, \"x\");", "__CPROVER_assert"),
    ("CPROVER_BUILTIN", "    LEANC_ASSUME(idx < len);", "LEANC_ASSUME"),
    ("CPROVER_BUILTIN", '    LEANC_REACH("here");', "LEANC_REACH"),
    ("CPROVER_BUILTIN", 'TOP:#include "leanc.h"', "include leanc.h"),
    ("CPROVER_BUILTIN", "TOP:#include <leanc.h>", "include <leanc.h>"),
    ("CPROVER_BUILTIN", 'TOP:#pragma CPROVER check disable "bounds"', "CBMC check pragma"),
    ("CPROVER_BUILTIN", '    _Pragma("CPROVER check disable \\"pointer\\"")', "CBMC _Pragma"),
    ("CPROVER_BUILTIN", "    #ifdef __CPROVER__\n    return SAFE_ARRAY_OK;\n    #endif", "__CPROVER__ conditional"),
    ("CPROVER_BUILTIN", "    __CPRO\\\nVER_assume(idx < len);", "identifier split by a line splice"),
    ("CPROVER_BUILTIN", 'TOP:%:include "leanc.h"', "digraph include"),
    ("CPROVER_BUILTIN", 'TOP:void my_assume(_Bool) __attribute__((weakref("__CPROVER_assume")));', "weakref alias"),
    ("ASSUMPTION_INJECTION", "    __builtin_assume(idx < len);", "__builtin_assume"),
    ("ASSUMPTION_INJECTION", "    if (idx >= len) __builtin_unreachable();", "__builtin_unreachable"),
    ("ASSUMPTION_INJECTION", "    __assume(idx < len);", "__assume"),
    ("ASSUMPTION_INJECTION", "    [[assume(idx < len)]];", "[[assume]]"),
    ("ASSUMPTION_INJECTION", "    __attribute__((assume(idx < len)));", "__attribute__((assume))"),
    ("ASSUMPTION_INJECTION", "    if (idx >= len) unreachable();", "C23 unreachable"),
    ("ASSUMPTION_INJECTION", "    arr = __builtin_assume_aligned(arr, 16);", "assume_aligned"),
    ("ASSUMPTION_INJECTION", "TOP:__attribute__((nonnull)) int helper(int *p);", "nonnull attribute"),
    ("WARNING_SUPPRESSION", 'TOP:#pragma clang diagnostic ignored "-Wsign-compare"', "pragma clang diagnostic"),
    ("WARNING_SUPPRESSION", "TOP:#pragma GCC diagnostic push", "pragma GCC diagnostic"),
    ("WARNING_SUPPRESSION", "TOP:  #  pragma   clang   diagnostic ignored \"-Wall\"", "spaced pragma"),
    ("WARNING_SUPPRESSION", '    _Pragma("GCC diagnostic ignored \\"-Wall\\"")', "_Pragma diagnostic"),
    ("WARNING_SUPPRESSION", "TOP:#pragma clang system_header", "system_header"),
    ("WARNING_SUPPRESSION", "    #ifndef __clang_analyzer__\n    *out = 0;\n    #endif", "analyzer conditional"),
    ("WARNING_SUPPRESSION", "    [[clang::suppress]] *out = 0;", "[[clang::suppress]]"),
    ("SANITIZER_SUPPRESSION", 'TOP:__attribute__((no_sanitize("address"))) static int h(void);', "no_sanitize"),
    ("SANITIZER_SUPPRESSION", "TOP:__attribute__((no_sanitize_address)) static int h(void);", "no_sanitize_address"),
    ("SANITIZER_SUPPRESSION", 'TOP:[[clang::no_sanitize("undefined")]] static int h(void);', "[[clang::no_sanitize]]"),
    ("SANITIZER_SUPPRESSION", "    #if defined(__SANITIZE_ADDRESS__)\n    return 0;\n    #endif", "__SANITIZE_ADDRESS__"),
    ("SANITIZER_SUPPRESSION", "    #if __has_feature(address_sanitizer)\n    return 0;\n    #endif", "__has_feature"),
    ("SANITIZER_SUPPRESSION", "    __lsan_disable();", "lsan runtime hook"),
    ("SANITIZER_SUPPRESSION", 'TOP:const char *__asan_default_options(void) { return "detect_leaks=0"; }', "asan options"),
    (
        "SANITIZER_SUPPRESSION",
        'TOP:_Pragma("clang attribute push(__attribute__((no_sanitize(\\"address\\"))), apply_to=function)")',
        "no_sanitize hidden in a _Pragma string",
    ),
    ("INCLUDE_ESCAPE", 'TOP:#include "/etc/passwd"', "absolute include"),
    ("INCLUDE_ESCAPE", 'TOP:#include "../harness/safe_array_get_harness.c"', "dotdot include"),
    ("INCLUDE_ESCAPE", "TOP:#include <../../etc/shadow>", "dotdot angle include"),
    ("INCLUDE_ESCAPE", 'TOP:#define HDR "x.h"\n#include HDR', "computed include"),
    ("INCLUDE_ESCAPE", 'TOP:static const char k[] = {\n#embed "/etc/hostname"\n};', "embed absolute"),
    ("DANGEROUS_CALL", '    system("rm -rf /");', "system"),
    ("DANGEROUS_CALL", '    popen("ls", "r");', "popen"),
    ("DANGEROUS_CALL", '    execl("/bin/sh", "sh", (char *)0);', "execl"),
    ("DANGEROUS_CALL", '    execvp("sh", 0);', "execvp"),
    ("DANGEROUS_CALL", "    fork();", "fork"),
    ("DANGEROUS_CALL", "    socket(2, 1, 0);", "socket"),
    ("DANGEROUS_CALL", "    connect(3, 0, 0);", "connect"),
    ("DANGEROUS_CALL", '    dlopen("libc.so.6", 1);', "dlopen"),
    ("DANGEROUS_CALL", "    ptrace(0, 0, 0, 0);", "ptrace"),
    ("DANGEROUS_CALL", "    syscall(59, 0, 0, 0);", "syscall"),
    ("DANGEROUS_CALL", '    (system)("ls");', "parenthesised designator"),
    ("DANGEROUS_CALL", "    int (*fp)(const char *) = system;", "function pointer"),
    ("DANGEROUS_CALL", "TOP:#define RUN system", "macro alias"),
    ("INLINE_ASM", '    asm("nop");', "asm"),
    ("INLINE_ASM", '    __asm__ __volatile__("" ::: "memory");', "__asm__"),
    ("INLINE_ASM", "    __asm volatile(\"nop\");", "__asm"),
    ("OBFUSCATION", "TOP:#define CAT(a, b) a##b", "token pasting"),
    ("OBFUSCATION", "    *out = 0; /* ??/ */", "trigraph"),
    ("OBFUSCATION", "    *out = 0; /* \u202e */", "bidi control"),
]


@pytest.mark.parametrize("rule, construct, label", CASES, ids=[c[2] for c in CASES])
def test_content_rule_flags_new_construct(spec, rule, construct, label):
    if construct.startswith(FILE_SCOPE_PREFIX):
        new = at_top(construct.removeprefix(FILE_SCOPE_PREFIX))
    else:
        new = inject(construct)
    d = cand(spec, new)
    assert rule in rules(d), (label, d.violations)
    v = next(v for v in d.violations if v.rule == rule)
    assert v.file == SRC and v.line is not None and v.line >= 1
    already_present_in_old = new.replace("if (idx > len) {", "if (idx >= len) {")
    d2 = cand(spec, already_present_in_old, old_src=new)
    forbidden_absolutely_by_integrity_rules = {v.rule for v in source_violations(spec, SRC, already_present_in_old)}
    assert rule not in rules(d2) or rule in forbidden_absolutely_by_integrity_rules, (label, d2.violations)
    assert rule in rules(check_generated(spec, {SRC: new}))


@pytest.mark.parametrize(
    "construct",
    [
        "    /* __CPROVER_assume(idx < len); */",
        "    // LEANC_ASSUME(idx < len); __builtin_assume(x); asm(\"nop\"); system(\"x\");",
        '    const char *msg = "__CPROVER_assume(idx < len) __builtin_unreachable() system(\\"ls\\")";',
        '    const char *m2 = "#pragma GCC diagnostic ignored no_sanitize __SANITIZE_ADDRESS__ asm";',
        "    char q = '#'; (void)q;",
        '    const char *inc = "#include \\"/etc/passwd\\" #include \\"leanc.h\\"";',
        "    /* #pragma clang diagnostic ignored \"-Wall\"\n       #include \"../x.h\"\n       fork(); */",
        "    #if 0\n    __CPROVER_assume(idx < len);\n    system(\"x\");\n    #endif",
        (
            "    int execute_step = 0, systemic = 1, asm_count = 2, my_fork = 3; (void)execute_step;"
            " (void)systemic; (void)asm_count; (void)my_fork;"
        ),
        '    /* the string "??" is not a trigraph */ const char *qq = "what?"; (void)qq;',
    ],
)
def test_comments_strings_and_lookalikes_are_not_flagged(spec, construct):
    new = inject(construct).replace("if (idx > len) {", "if (idx >= len) {")
    d = cand(spec, new)
    assert d.allowed, d.violations
    assert check_generated(spec, {SRC: new}).allowed


def test_member_calls_named_like_dangerous_functions_are_not_flagged(spec):
    top = "struct ops { int (*connect)(int); int (*system)(int); };\nstatic int use(struct ops *o, struct ops s) { return o->connect(1) + s.system(2); }"
    d = cand(spec, at_top(top).replace("if (idx > len) {", "if (idx >= len) {"))
    assert d.allowed, d.violations


def test_ordinary_includes_are_fine(spec):
    new = at_top('#include <stdio.h>\n#include <sys/types.h>\n#include <safe_array.h>')
    assert cand(spec, new.replace("if (idx > len) {", "if (idx >= len) {")).allowed


def test_quoted_include_must_be_a_protected_header(spec):
    new = at_top('#include "sub/dir/helper.h"').replace("if (idx > len) {", "if (idx >= len) {")
    d = cand(spec, new)
    assert rules(d) == {"INCLUDE_ESCAPE"} and "protected header" in d.violations[0].message


def test_count_based_more_occurrences_is_flagged_with_the_new_line(spec):
    one = at_top('#pragma GCC diagnostic ignored "-Wall"')
    two = at_top('#pragma GCC diagnostic ignored "-Wall"\n#pragma GCC diagnostic ignored "-Wextra"')
    d = cand(spec, two, old_src=one)
    (v,) = [v for v in d.violations if v.rule == "WARNING_SUPPRESSION"]
    assert v.line == 3 and "-Wextra" in v.message, "the added line is shown as written"
    assert "1 -> 2" in v.message


def test_violation_line_numbers_point_at_the_new_code(spec):
    new = inject("    system(\"x\");")
    (v,) = [v for v in cand(spec, new).violations if v.rule == "DANGEROUS_CALL"]
    assert new.splitlines()[v.line - 1].strip() == 'system("x");'
    assert 'system("x")' in v.message


def test_messages_escape_bidi_controls(spec):
    (v,) = cand(spec, inject("    *out = 0; /* \u202e */")).violations
    assert "\u202e" not in v.message and "\\u202e" in v.message


WITH_ASSERTS = BUGGY.replace(
    "    *out = arr[idx];",
    "    assert(idx < len);\n    _Static_assert(sizeof(int) >= 2, \"int\");\n    *out = arr[idx];",
)


def test_assert_removed(spec):
    d = cand(spec, WITH_ASSERTS.replace("    assert(idx < len);\n", ""), old_src=WITH_ASSERTS)
    assert "ASSERT_REMOVED" in rules(d)
    assert "assert()" in next(v.message for v in d.violations if v.rule == "ASSERT_REMOVED")


def test_static_assert_removed(spec):
    d = cand(spec, WITH_ASSERTS.replace('    _Static_assert(sizeof(int) >= 2, "int");\n', ""), old_src=WITH_ASSERTS)
    assert "ASSERT_REMOVED" in rules(d)


def test_assert_commented_out_or_moved_into_if0_counts_as_removed(spec):
    for new in (
        WITH_ASSERTS.replace("    assert(idx < len);", "    /* assert(idx < len); */"),
        WITH_ASSERTS.replace("    assert(idx < len);", "#if 0\n    assert(idx < len);\n#endif"),
        WITH_ASSERTS.replace("    assert(idx < len);", '    const char *s = "assert(idx < len);"; (void)s;'),
    ):
        assert "ASSERT_REMOVED" in rules(cand(spec, new, old_src=WITH_ASSERTS)), new


def test_assert_disabled_by_ndebug_or_redefinition(spec):
    for top in ("#define NDEBUG", "#undef assert\n#define assert(x) ((void)0)"):
        assert "ASSERT_REMOVED" in rules(cand(spec, at_top(top))), top


def test_assert_kept_or_added_is_fine(spec):
    fixed = WITH_ASSERTS.replace("if (idx > len) {", "if (idx >= len) {")
    assert cand(spec, fixed, old_src=WITH_ASSERTS).allowed
    more = fixed.replace("    *out = arr[idx];", "    assert(arr != NULL);\n    *out = arr[idx];")
    assert cand(spec, more, old_src=WITH_ASSERTS).allowed
    assert_only_in_a_comment = BUGGY.replace("    *out = arr[idx];", "    /* assert(idx < len); */\n    *out = arr[idx];")
    assert cand(spec, GOOD, old_src=assert_only_in_a_comment).allowed


TRIVIAL_BODIES = [
    "    return SAFE_ARRAY_OK;",
    "    return 0;",
    "    return -1;",
    "    return (int)0;",
    "    return (SAFE_ARRAY_ERR_RANGE);",
    "    (void)arr; (void)len; (void)idx; (void)out;\n    return SAFE_ARRAY_OK;",
    "    { return 0; }",
    "",
    "    abort();",
    "    (void)(arr);\n    ;\n    return SAFE_ARRAY_ERR_NULL;;",
    "    (void)arr; (void)len; (void)idx; (void)out;\n" * 300 + "    return SAFE_ARRAY_OK;",
]


@pytest.mark.parametrize("body", TRIVIAL_BODIES)
def test_trivialized_target(spec, body):
    new = '#include "safe_array.h"\n#include <stdlib.h>\n\nint safe_array_get(const int *arr, size_t len, size_t idx, int *out)\n{\n' + body + "\n}\n"
    d = cand(spec, new)
    assert "TRIVIALIZED_TARGET" in rules(d), d.violations
    v = next(v for v in d.violations if v.rule == "TRIVIALIZED_TARGET")
    assert v.file == SRC and v.line == 4
    assert "TRIVIALIZED_TARGET" in rules(check_generated(spec, {SRC: new}))


def test_target_definition_removed_or_hidden(spec):
    proto_only = '#include "safe_array.h"\nint other(void) { return 1; }\n'
    assert "TRIVIALIZED_TARGET" in rules(cand(spec, proto_only))
    in_if0 = "#if 0\n" + BUGGY + "#endif\nint other(void) { return 1; }\n"
    assert "TRIVIALIZED_TARGET" in rules(cand(spec, in_if0))
    as_macro = '#include "safe_array.h"\n#define safe_array_get(a, l, i, o) 0\n'
    d = cand(spec, as_macro)
    assert "TRIVIALIZED_TARGET" in rules(d)
    assert any("macro" in v.message for v in d.violations)


def test_target_moved_between_editable_files_is_fine(spec2):
    old = {SRC: BUGGY, "src/helper.c": "int helper_value = 1;\n"}
    new = {SRC: '#include "safe_array.h"\nint helper_value = 1;\n', "src/helper.c": GOOD}
    assert check_candidate(spec2, old, new).allowed


def test_already_trivial_target_is_not_blamed_on_the_repair(spec):
    stub = '#include "safe_array.h"\nint safe_array_get(const int *arr, size_t len, size_t idx, int *out)\n{\n    return 0;\n}\n'
    stub2 = stub + "/* still a stub */\n"
    assert "TRIVIALIZED_TARGET" not in rules(cand(spec, stub2, old_src=stub))


def test_nontrivial_bodies_are_not_trivial():
    for body in (
        "if (arr == NULL) return -1; return 0;",
        "*out = arr[idx]; return 0;",
        "return arr[idx];",
        "return len > idx ? 0 : -2;",
        "return (x) + 1;",
        "return helper(arr, len);",
        "(void)compute(arr); return 0;",
        "if (x) (void)y; return 0;",
    ):
        assert not is_trivial_body(body), body


def test_find_function_definitions_ignores_calls_prototypes_and_nested():
    text = (
        "int f(int x);\n"
        "#define CALL f(1)\n"
        "static int g(void) { return f(2); }\n"
        "int f(int x) __attribute__((unused))\n{\n    return x + 1;\n}\n"
        "struct s { int (*f)(int); };\n"
    )
    defs = find_function_definitions(CSource(text), "f", "a.c")
    assert [(d.file, d.line, d.trivial) for d in defs] == [("a.c", 4, False)]


def test_check_generated_missing_target_and_paths(spec):
    proto_only = '#include "safe_array.h"\n/* safe_array_get is declared in the header */\n'
    d = check_generated(spec, {SRC: proto_only})
    assert rules(d) == {"MISSING_TARGET"}
    d = check_generated(spec, {"harness/safe_array_get_harness.c": GOOD})
    assert rules(d) == {"PATH_NOT_EDITABLE", "MISSING_TARGET"}
    d = check_generated(spec, {SRC: GOOD, "../evil.c": "int x;\n"})
    assert rules(d) == {"PATH_INVALID"}
    assert rules(check_generated(spec, {})) == {"MISSING_TARGET"}


def test_check_generated_target_in_any_editable_file(spec2):
    files = {SRC: '#include "safe_array.h"\n', "src/helper.c": GOOD}
    assert check_generated(spec2, files).allowed


def test_check_generated_content_rules(spec):
    bad = GOOD.replace("    *out = arr[idx];", '    __CPROVER_assume(idx < len);\n    system("x");\n    *out = arr[idx];')
    assert rules(check_generated(spec, {SRC: bad})) >= {"CPROVER_BUILTIN", "DANGEROUS_CALL"}


def test_decision_is_deterministic_and_deduplicated(spec):
    new = inject('    __CPROVER_assume(idx < len);\n    system("a");\n    asm("nop");')
    d1, d2 = cand(spec, new), cand(spec, new)
    assert d1 == d2
    order = list(RULES)
    idx = [order.index(v.rule) for v in d1.violations]
    assert idx == sorted(idx)
    keys = [(v.rule, v.file, v.line, v.message) for v in d1.violations]
    assert len(keys) == len(set(keys))
    assert all(v.message for v in d1.violations)


def test_repair_loop_cheat_from_test_suite_is_rejected(spec):
    cheat = BUGGY.replace("if (idx > len) {", "__CPROVER_assume(idx < len);\n    if (idx > len) {")
    assert "CPROVER_BUILTIN" in rules(cand(spec, cheat))
    broken = BUGGY.replace("return SAFE_ARRAY_OK;", "return SAFE_ARRAY_OK")
    assert cand(spec, broken).allowed, "a compile error is the verifier's business, not the policy's"
