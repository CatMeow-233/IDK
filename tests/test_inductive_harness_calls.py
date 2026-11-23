from __future__ import annotations

import pytest

from leanc.backends.inductive.harness_calls import (
    ASSUME_AFTER_CLAIMED_CALL_LABEL,
    CLAIMED_CALLS_COUNTER,
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
RESET_STATEMENT = f" {CLAIMED_CALLS_COUNTER} = 0; "


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
        "    struct ops o;\n    o.step = 0;\n    struct ops *p = &o;\n    p -> step = 1;\n    step(0);\n    reset(0);\n",
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


def test_preprocessed_harness_must_start_with_a_linemarker():
    pp = "int pad;\n" + preprocessed("    step(0);\n    reset(0);\n")
    assert any("does not start with a linemarker" in problem for problem in problems_of(pp))


def test_verify_instrumentation_resets_counts_and_checks_assumes():
    layout = harness_layout(BRANCHES, CLAIMED, HARNESS)
    text = instrumented_harness(BRANCHES, layout, HARNESS)
    first_line, declaration, rest = text.split("\n", 2)
    assert first_line == f'# 1 "{HARNESS}"'
    assert declaration == f"static int {CLAIMED_CALLS_COUNTER};"
    assert rest.count("\n") == BRANCHES.count("\n")
    assert "{" + RESET_STATEMENT + "\n    int s;" in rest
    check = f'__CPROVER_assert({CLAIMED_CALLS_COUNTER} == 0, "{claimed_call_label("step")}")'
    assert f"int r = ({check}, {CLAIMED_CALLS_COUNTER} = 1, step(&s));" in rest
    assert f"    {checked_assume('s >= 0')};\n" in rest
    assert "leanc_return" not in rest


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
