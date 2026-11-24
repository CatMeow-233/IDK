from __future__ import annotations

import pytest

from leanc.backends.inductive.harness_narrowing import EARLY_EXIT_RULE, PRE_CALL_WRITE_RULE
from leanc.backends.inductive.harness_calls import (
    ASSUME_AFTER_CLAIMED_CALL_LABEL,
    CLAIMED_CALLS_COUNTER,
    FREE_ANCHOR_CALL,
    FREE_DECLARATION,
    ReturnKind,
    body_references,
    claimed_call_label,
    harness_layout,
    instrumented_harness,
    return_probe_label,
)
from leanc.rules.loop_sites import Origin, origin_lines

HARNESS = "harness/inductive.c"
HEADER = "include/api.h"
CLAIMED = ("step", "reset")
MARKER = f'# 1 "{HARNESS}"\n'
HEADER_MARKER = f'# 1 "{HEADER}" 1\n'
BACK_MARKER = f'# 3 "{HARNESS}" 2\n'
PROTOTYPES = "int step(int *s);\nvoid reset(int *s);\n"
RESET_STATEMENT = f" {CLAIMED_CALLS_COUNTER} = 0; {FREE_ANCHOR_CALL} "


def preprocessed(body: str, *, before: str = "", header: str = PROTOTYPES) -> str:
    return MARKER + HEADER_MARKER + header + BACK_MARKER + before + "int main(void)\n{\n" + body + "}\n"


BRANCHES = preprocessed(
    "    int s;\n"
    "    __CPROVER_assume(s >= 0);\n"
    "    if (s) {\n"
    "        int r = step(&s);\n"
    "        __CPROVER_assert(r >= 0, \"r\");\n"
    "    } else {\n"
    "        reset(&s);\n"
    "    }\n"
    "    return 0;\n"
)


def problems_of(pp: str, claimed: tuple[str, ...] = CLAIMED) -> tuple[str, ...]:
    return harness_layout(pp, claimed, HARNESS).problems


def line_of(pp: str, needle: str) -> Origin | None:
    return origin_lines(pp)[pp[: pp.index(needle)].count("\n")]


def test_layout_finds_direct_claimed_calls_and_assumes():
    layout = harness_layout(BRANCHES, CLAIMED, HARNESS)
    assert layout.problems == ()
    assert [call.function for call in layout.calls] == ["step", "reset"]
    assert [call.origin for call in layout.calls] == [line_of(BRANCHES, "step(&s)"), line_of(BRANCHES, "reset(&s)")]
    assert len(layout.assumes) == 1
    assert layout.main_body_open == BRANCHES.index("{\n    int s;")


def test_header_prototypes_are_not_calls():
    layout = harness_layout(BRANCHES, CLAIMED, HARNESS)
    assert all(call.origin is not None and call.origin.file == HARNESS for call in layout.calls)


def test_member_named_like_a_claimed_function_is_not_a_call():
    pp = preprocessed(
        "    step(0);\n    struct ops o;\n    o.step = 0;\n    struct ops *p = &o;\n    p -> step = 1;\n    reset(0);\n",
        header=PROTOTYPES + "struct ops { int step; };\n",
    )
    layout = harness_layout(pp, CLAIMED, HARNESS)
    assert layout.problems == ()
    assert [call.function for call in layout.calls] == ["step", "reset"]


DIRECT_CALL_ONLY = "used other than in a direct call"
UNRECOGNISED_BLOCK = "neither a recognised function body"


@pytest.mark.parametrize(
    "body, before, needle",
    [
        ("    int (*f)(int *) = step;\n    f(0);\n    reset(0);\n", "", DIRECT_CALL_ONLY),
        ("    table[0](0);\n    reset(0);\n", "static int (*table[])(int *) = { step };\n", UNRECOGNISED_BLOCK),
        ("    p(0);\n    reset(0);\n", "static int (*p)(int *) = step;\n", DIRECT_CALL_ONLY),
        ("    int step = 1;\n    reset(&step);\n", "", DIRECT_CALL_ONLY),
    ],
)
def test_claimed_function_used_other_than_in_a_direct_call_is_a_problem(body, before, needle):
    problems = problems_of(preprocessed(body, before=before))
    assert any(needle in problem for problem in problems)


def test_uncalled_claimed_function_is_a_problem():
    problems = problems_of(preprocessed("    step(0);\n"))
    assert problems == ("claimed function reset is never called from the harness",)


def test_assume_after_a_claimed_call_in_the_same_function_is_a_problem():
    pp = preprocessed("    int s;\n    step(&s);\n    __CPROVER_assume(s < 16);\n    reset(&s);\n")
    problems = problems_of(pp)
    assert len(problems) == 1
    assert "LEANC_ASSUME after the call of the claimed function step" in problems[0]


def test_claimed_call_inside_an_assumption_is_a_problem():
    problems = problems_of(preprocessed("    int s;\n    __CPROVER_assume(step(&s) == 0);\n    reset(&s);\n"))
    assert len(problems) == 1
    assert "claimed function step is called inside LEANC_ASSUME" in problems[0]


def assume_check() -> str:
    return f'__CPROVER_assert({CLAIMED_CALLS_COUNTER} == 0, "{ASSUME_AFTER_CLAIMED_CALL_LABEL}")'


def checked_assume(condition: str) -> str:
    return f"__CPROVER_assume(({{ _Bool leanc_assumed = ({condition}); {assume_check()}; leanc_assumed; }}))"


def test_assumption_check_runs_after_the_condition_and_before_the_assumption():
    helper = "static int small(int *s)\n{\n    return step(s) < 16;\n}\n"
    pp = preprocessed("    int s;\n    __CPROVER_assume(small(&s));\n    reset(&s);\n", before=helper)
    layout = harness_layout(pp, CLAIMED, HARNESS)
    assert layout.problems == ()
    text = instrumented_harness(pp, layout, HARNESS)
    assert f"    {checked_assume('small(&s)')};\n" in text
    assert text.count(ASSUME_AFTER_CLAIMED_CALL_LABEL) == 1


@pytest.mark.parametrize("arguments", ["s > 0, s < 16", "(s > 0), s < 16", "s > 0,"])
def test_assumption_with_more_than_one_argument_is_a_problem(arguments):
    pp = preprocessed(f"    int s;\n    __CPROVER_assume({arguments});\n    step(&s);\n    reset(&s);\n")
    assert problems_of(pp) == (f"{HARNESS}:6: __CPROVER_assume must have exactly one argument",)


@pytest.mark.parametrize("condition", ["f(s, 1) > 0", "(int[]){s, 1}[0] > 0", "s > 0 ? 1 : 0"])
def test_commas_nested_in_the_assumption_argument_are_allowed(condition):
    pp = preprocessed(f"    int s;\n    __CPROVER_assume({condition});\n    step(&s);\n    reset(&s);\n")
    assert problems_of(pp) == ()


@pytest.mark.parametrize(
    "before",
    [
        "int helper(a)\nint a;\n{\n    return step(&a);\n}\n",
        "int (*pick(int k))(int *)\n{\n    __CPROVER_assume(k > 0);\n    return step;\n}\n",
        "int (*pick(int k))(int *)\n{\n    return main();\n}\n",
        "static int table[] = { sizeof(step(0)) };\n",
    ],
)
def test_names_inside_unrecognised_blocks_are_problems(before):
    pp = preprocessed("    step(0);\n    reset(0);\n", before=before)
    assert any(UNRECOGNISED_BLOCK in problem for problem in problems_of(pp))


def test_assume_in_a_helper_defined_before_the_call_is_left_to_the_dynamic_check():
    helper = "static void narrow(int *s)\n{\n    __CPROVER_assume(*s < 16);\n}\n"
    pp = preprocessed("    int s;\n    step(&s);\n    narrow(&s);\n    reset(&s);\n", before=helper)
    layout = harness_layout(pp, CLAIMED, HARNESS)
    assert layout.problems == ()
    assert len(layout.assumes) == 1


def test_claimed_call_inside_a_helper_defined_before_main_does_not_block_main_assumes():
    helper = "static int call_step(int *s)\n{\n    return step(s);\n}\n"
    pp = preprocessed("    int s;\n    __CPROVER_assume(s >= 0);\n    call_step(&s);\n    reset(&s);\n", before=helper)
    assert problems_of(pp) == ()


@pytest.mark.parametrize(
    "pp, expected",
    [
        (MARKER + HEADER_MARKER + PROTOTYPES + BACK_MARKER + "void helper(void) { step(0); reset(0); }\n", "0 times"),
        (preprocessed("    step(0);\n    reset(0);\n") + "int main(void) { return 0; }\n", "2 times"),
        (preprocessed("    step(0);\n    reset(0);\n    main();\n"), "main is referenced inside a function body"),
    ],
)
def test_entry_point_rules(pp, expected):
    assert any(expected in problem for problem in problems_of(pp))


def test_main_defined_outside_the_harness_file_is_a_problem():
    pp = MARKER + HEADER_MARKER + PROTOTYPES + "int main(void) { step(0); reset(0); return 0; }\n" + BACK_MARKER
    assert any(f"not in the harness {HARNESS}" in problem for problem in problems_of(pp))


@pytest.mark.parametrize("name", ["leanc_returned", "LEANC_claimed_calls", "leanc_x"])
def test_harness_identifiers_with_the_reserved_prefix_are_a_problem(name):
    pp = preprocessed(f"    int {name} = 0;\n    step(&{name});\n    reset(0);\n")
    assert any(f"{name} uses the leanc_ prefix" in problem for problem in problems_of(pp))


def test_probe_labels_in_string_literals_are_not_identifiers():
    pp = preprocessed('    step(0);\n    __CPROVER_assert(0, "leanc_reach:done");\n    reset(0);\n')
    assert problems_of(pp) == ()


TERMINATION_RULE = "ends the path or jumps out of it without a check"


@pytest.mark.parametrize("call", ["exit(0)", "abort()", "_Exit(1)", "quick_exit(0)", "longjmp(env, 1)"])
def test_a_path_ending_call_after_the_claimed_call_is_a_problem(call):
    pp = preprocessed(f"    int s = step(0);\n    if (s) {{\n        {call};\n    }}\n    reset(&s);\n")
    (problem,) = [problem for problem in problems_of(pp) if TERMINATION_RULE in problem]
    assert problem.startswith(f"{HARNESS}:") and call.partition("(")[0] in problem


def test_a_path_ending_call_in_a_header_helper_is_a_problem_but_its_prototype_is_not():
    header = PROTOTYPES + "void abort(void);\nstatic inline void bail(void) { abort(); }\n"
    pp = preprocessed("    step(0);\n    reset(0);\n    bail();\n", header=header)
    assert [problem for problem in problems_of(pp) if TERMINATION_RULE in problem] == [
        f"{HEADER}:4: abort {TERMINATION_RULE}; CBMC prunes such a path like an assumption, so the inductive "
        "harness and the code it includes must not use it (not even as a function pointer value)"
    ]


def test_members_and_prototypes_named_like_termination_calls_are_not_problems():
    header = PROTOTYPES + "void exit(int);\nstruct opts { int exit; };\n"
    pp = preprocessed("    struct opts o;\n    int r = step(0);\n    o.exit = r;\n    reset(&o.exit);\n", header=header)
    assert problems_of(pp) == ()


@pytest.mark.parametrize(
    "value", ["exit", "&abort", "(_Exit)", "longjmp"], ids=["plain", "address", "parenthesised", "longjmp"]
)
def test_a_termination_function_taken_as_a_file_scope_value_is_a_problem(value):
    before = f"static void (*const bail)(int) = {value};\n"
    pp = preprocessed("    int s = step(0);\n    if (s) {\n        bail(0);\n    }\n    reset(&s);\n", before=before)
    (problem,) = [problem for problem in problems_of(pp) if TERMINATION_RULE in problem]
    assert problem.startswith(f"{HARNESS}:3: {value.strip('&()')} ")


def test_a_struct_tag_named_like_a_termination_call_is_not_a_problem():
    header = PROTOTYPES + "struct sigaction { int flags; };\nint sigaction(int sig, const struct sigaction *act);\n"
    pp = preprocessed("    step(0);\n    reset(0);\n", header=header)
    assert problems_of(pp) == ()


def exit_problems(body: str, *, before: str = "") -> list[str]:
    return [problem for problem in problems_of(preprocessed(body, before=before)) if EARLY_EXIT_RULE in problem]


@pytest.mark.parametrize(
    "body",
    [
        "    int s = step(0);\n    if (s) {\n        return 1;\n    }\n    reset(&s);\n    return 0;\n",
        "    int s = step(0);\n    reset(&s);\n    if (s) return 1;\n",
        "    int s = step(0);\n    reset(&s);\n    {\n        return 0;\n    }\n",
        "    int s = step(0);\n    reset(&s);\n    if (s) {\n    } else return 0;\n",
        "    int s = step(0);\n    reset(&s);\n    return 0;\n    s = 1;\n",
        "    int s = step(0);\n    if (s) {\n        goto done;\n    }\n    reset(&s);\ndone:\n    return 0;\n",
    ],
    ids=["return_in_branch", "conditional_last_return", "return_in_block", "else_return", "statement_after_return", "goto"],
)
def test_a_return_or_goto_that_can_skip_assertions_is_a_problem(body):
    problems = exit_problems(body)
    assert problems and all(problem.startswith(f"{HARNESS}:") for problem in problems)
    assert any(("goto" if "goto" in body else "return") + " " in problem for problem in problems)


def test_one_final_return_and_returns_in_helpers_are_not_problems():
    helper = "static int pick(int s)\n{\n    if (s) {\n        return 1;\n    }\n    goto out;\nout:\n    return 0;\n}\n"
    assert exit_problems("    int s = step(0);\n    reset(&s);\n    return pick(s);\n", before=helper) == []
    assert exit_problems("    int s = step(0);\n    reset(&s);\n") == []


def write_problems(body: str, *, before: str = "") -> list[str]:
    return [problem for problem in problems_of(preprocessed(body, before=before)) if PRE_CALL_WRITE_RULE in problem]


ASSUMED = "    int s = nondet_int();\n    __CPROVER_assume(s >= 0);\n"


@pytest.mark.parametrize(
    "write, operator",
    [
        ("    s = s % 8;\n", "="),
        ("    s %= 8;\n", "%="),
        ("    s <<= 1;\n", "<<="),
        ("    s++;\n", "++"),
        ("    --s;\n", "--"),
        ("    if (s > 7) s = 0;\n", "="),
        ("    (void)(s %= 8);\n", "%="),
        ("    int t = (s = 1);\n", "="),
        ("    int a = 1, b = (s = 2);\n", "="),
        ("    for (int k = 0; k < 1; k++) {\n    }\n", "++"),
    ],
    ids=["assign", "compound", "shift", "increment", "decrement", "conditional", "cast", "initialiser", "second_declarator", "for_step"],
)
def test_a_write_between_the_last_assume_and_the_claimed_call_is_a_problem(write, operator):
    problems = write_problems(ASSUMED + write + "    step(&s);\n    reset(&s);\n")
    assert problems and all(problem.startswith(f"{HARNESS}:") for problem in problems)
    assert any(f": {operator} writes in main()" in problem for problem in problems)
    assert all("claimed function step at harness/inductive.c:" in problem for problem in problems)


def test_a_write_inside_the_claimed_call_arguments_is_a_problem():
    (problem,) = write_problems(ASSUMED + "    step((s = 1, &s));\n    reset(&s);\n")
    assert ": = writes in main()" in problem


def test_declarations_comparisons_and_writes_outside_the_window_are_not_problems():
    body = (
        "    int s = nondet_int();\n    s = s + 1;\n    __CPROVER_assume(s >= 0);\n"
        "    int t = s == 1 && s <= 2 && s >= 0 && s != 3;\n    struct ops *p = 0;\n    unsigned char buf[2] = {0};\n"
        "    if (t) {\n        int r = step(&s);\n        s = r;\n    }\n    s++;\n    reset(&s);\n"
    )
    assert write_problems(body, before="struct ops { int x; };\n") == []


def test_writes_in_main_are_not_checked_when_main_calls_no_claimed_function_directly():
    helper = "static int wrapped(int *s)\n{\n    return step(s);\n}\n"
    body = "    int s = nondet_int();\n    __CPROVER_assume(s >= 0);\n    s = 1;\n    wrapped(&s);\n"
    assert write_problems(body, before=helper) == []


def test_preprocessed_harness_must_start_with_a_linemarker():
    pp = "int pad;\n" + preprocessed("    step(0);\n    reset(0);\n")
    assert any("does not start with a linemarker" in problem for problem in problems_of(pp))


def test_verify_instrumentation_resets_counts_and_checks_assumes():
    layout = harness_layout(BRANCHES, CLAIMED, HARNESS)
    text = instrumented_harness(BRANCHES, layout, HARNESS)
    first_line, declaration, free_declaration, rest = text.split("\n", 3)
    assert first_line == f'# 1 "{HARNESS}"'
    assert declaration == f"static int {CLAIMED_CALLS_COUNTER};"
    assert free_declaration == FREE_DECLARATION == "void free(void *);"
    assert rest.count("\n") == BRANCHES.count("\n")
    assert "{" + RESET_STATEMENT + "\n    int s;" in rest
    check = f'__CPROVER_assert({CLAIMED_CALLS_COUNTER} == 0, "{claimed_call_label("step")}")'
    assert f"int r = ({check}, {CLAIMED_CALLS_COUNTER} = 1, step(&s));" in rest
    assert f"    {checked_assume('s >= 0')};\n" in rest
    assert "leanc_return" not in rest


def test_main_starts_with_a_no_op_free_so_dfcc_links_the_free_model():
    text = instrumented_harness(BRANCHES, harness_layout(BRANCHES, CLAIMED, HARNESS), HARNESS)
    assert FREE_ANCHOR_CALL == "free((void *)0);"
    assert text.count(FREE_ANCHOR_CALL) == 1
    assert text.index(FREE_DECLARATION) < text.index("int main(void)") < text.index(FREE_ANCHOR_CALL)
    assert "int main(void)\n{" + RESET_STATEMENT in text


def test_vacuity_instrumentation_probes_value_and_void_returns():
    layout = harness_layout(BRANCHES, CLAIMED, HARNESS)
    text = instrumented_harness(BRANCHES, layout, HARNESS, {"step": ReturnKind.VALUE, "reset": ReturnKind.VOID})
    step_probe = f'__CPROVER_assert(0, "{return_probe_label("step")}")'
    reset_probe = f'__CPROVER_assert(0, "{return_probe_label("reset")}")'
    assert f"({{ __auto_type leanc_returned = step(&s); {step_probe}; leanc_returned; }}));" in text
    assert f"reset(&s), {reset_probe});" in text


def test_nested_claimed_calls_get_separate_wrappers():
    pp = preprocessed("    int s;\n    step((reset(&s), &s));\n")
    layout = harness_layout(pp, CLAIMED, HARNESS)
    assert layout.problems == ()
    text = instrumented_harness(pp, layout, HARNESS)
    assert text.count(f"{CLAIMED_CALLS_COUNTER} = 1, ") == 2
    assert text.index(claimed_call_label("step")) < text.index(claimed_call_label("reset"))


def test_call_right_after_the_main_brace_keeps_the_reset_first():
    pp = MARKER + HEADER_MARKER + PROTOTYPES + BACK_MARKER + "int main(void){step(0); reset(0); return 0;}\n"
    layout = harness_layout(pp, CLAIMED, HARNESS)
    text = instrumented_harness(pp, layout, HARNESS)
    assert "{" + RESET_STATEMENT + f'(__CPROVER_assert({CLAIMED_CALLS_COUNTER} == 0' in text


def test_instrumenting_a_rejected_layout_raises():
    pp = preprocessed("    step(0);\n")
    with pytest.raises(ValueError):
        instrumented_harness(pp, harness_layout(pp, CLAIMED, HARNESS), HARNESS)


def test_body_references_ignore_file_scope_and_members():
    pp = preprocessed("    o.main = 1;\n    return main();\n", before="int main(void);\n", header="struct o { int main; };\n")
    assert body_references(pp, "main") == [f"{HARNESS}:{line_of(pp, 'return main').line}"]
