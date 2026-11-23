from __future__ import annotations

import pytest
from loop_site_support import (
    OTHER_REL,
    REL,
    contract_at,
    only_problem_kind,
    preprocessed,
    problem_kinds,
    sites_of,
)

from leanc.rules.loop_sites import (
    FUNCTION_PROBE_PREFIX,
    LOOP_PROBE_PREFIX,
    LoopSiteMatch,
    SiteProblemKind,
    loop_sites,
)

FOR_LOOP = """void f(int n)
{
    for (int i = 0; i < n; i++) {
        n--;
    }
}
"""
MULTI_LINE_HEADER = """void f(int n)
{
    int i = 0;
    while (i <
           n)
    {
        i++;
    }
}
"""
MARKER_INSIDE_HEADER = """void f(int *p)
{
    while (p !=
# 3 "src/loops.c" 3 4
               ((void *)0)
# 3 "src/loops.c"
               ) {
        p = 0;
    }
}
"""
MARKER_INSIDE_HEADER_SOURCE = """void f(int *p)
{
    while (p != NULL) {
        p = 0;
    }
}
"""
SAME_ORIGIN_ON_TWO_LINES = """void f(int n)
{
    while (n) { n--; }
# 3 "src/loops.c"
    for (;;) { }
}
"""
MARKER_BEFORE_BODY = """void f(int n)
{
    while (n > 0)
# 13 "src/loops.c"
    {
        n--;
    }
}
"""
MARKER_BEFORE_HEADER = """void f(int n)
{
    for
# 13 "src/loops.c"
    (; n < 3; n++) {
    }
}
"""
SPLICED = """void f(int n)
{
    int k = 1 + \\
            2;
    while (n) { n--; }
}
"""
HEADER_IN_OTHER_FILE = """void f(int n)
{
    while (n > 0
# 1 "include/close.h" 1
    ) {
# 4 "src/loops.c" 2
        n--;
    }
}
"""
LOOP_IN_INCLUDED_HEADER = """# 1 "include/h.h" 1
static void g(int n) { while (n) { n--; } }
# 2 "src/loops.c" 2
int x;
"""
LOOP_IN_INCLUDED_HEADER_SOURCE = '#include "h.h"\nint x;\n'


def _body(*lines: str) -> str:
    return "void f(int n)\n{\n" + "".join(f"    {line}\n" for line in lines) + "}\n"


def test_a_for_loop_site_records_its_keyword_header_and_body():
    pp = preprocessed(FOR_LOOP)
    match = loop_sites(REL, pp, FOR_LOOP, [contract_at(3)])
    assert match.accepted and not match.problems
    (matched,) = match.matched
    site = matched.site
    assert matched.contract == contract_at(3)
    assert (site.file, site.line, site.keyword, site.header_end_line) == (REL, 3, "for", 3)
    assert site.header_close == pp.index(") {")
    assert site.body_open == pp.index("{\n        n--")
    assert (site.clause_offset, site.body_offset) == (site.header_close + 1, site.body_open + 1)


def test_a_multi_line_header_records_the_line_where_it_ends():
    (matched,) = sites_of(MULTI_LINE_HEADER, 4).matched
    assert (matched.site.keyword, matched.site.line, matched.site.header_end_line) == ("while", 4, 5)
    assert preprocessed(MULTI_LINE_HEADER)[matched.site.body_open] == "{"


def test_linemarkers_inside_a_header_keep_the_origin_line():
    pp = preprocessed(MARKER_INSIDE_HEADER)
    (matched,) = loop_sites(REL, pp, MARKER_INSIDE_HEADER_SOURCE, [contract_at(3)]).matched
    assert (matched.site.line, matched.site.header_end_line) == (3, 3)
    assert pp[matched.site.header_close] == ")" and pp[matched.site.body_open] == "{"


def test_a_linemarker_between_the_header_and_the_body_is_skipped():
    pp = preprocessed(MARKER_BEFORE_BODY)
    (matched,) = sites_of(MARKER_BEFORE_BODY, 3).matched
    assert (matched.site.line, matched.site.header_end_line) == (3, 3)
    assert matched.site.body_open == pp.index("{\n        n--")


def test_a_linemarker_between_the_keyword_and_the_header_is_skipped():
    pp = preprocessed(MARKER_BEFORE_HEADER)
    (matched,) = sites_of(MARKER_BEFORE_HEADER, 3).matched
    assert (matched.site.keyword, matched.site.line, matched.site.header_end_line) == ("for", 3, 13)
    assert matched.site.header_close == pp.index(") {")


def test_a_line_without_a_loop_is_rejected():
    assert only_problem_kind(FOR_LOOP, 2) is SiteProblemKind.NO_LOOP
    assert only_problem_kind(FOR_LOOP, 4) is SiteProblemKind.NO_LOOP


@pytest.mark.parametrize(
    "line",
    ['const char *s = "while (n) {";', "/* for (;;) { } */", "int forward = 0, dowhile = 1, gotox = 2;"],
)
def test_a_keyword_in_a_literal_a_comment_or_an_identifier_is_not_a_loop(line):
    assert only_problem_kind(_body(line), 3) is SiteProblemKind.NO_LOOP


def test_a_loop_of_another_file_on_the_same_line_number_is_not_matched():
    pp = preprocessed(LOOP_IN_INCLUDED_HEADER)
    match = loop_sites(REL, pp, LOOP_IN_INCLUDED_HEADER_SOURCE, [contract_at(1)])
    assert problem_kinds(match) == [SiteProblemKind.NO_LOOP]


def test_text_before_any_linemarker_belongs_to_no_file():
    match = loop_sites(REL, FOR_LOOP, FOR_LOOP, [contract_at(3)])
    assert problem_kinds(match) == [SiteProblemKind.NO_LOOP]


@pytest.mark.parametrize(
    "line",
    ["for (int i = 0; i < n; i++) { n--; } while (n) { n--; }", "do { n--; } while (n > 0);"],
)
def test_two_loop_keywords_on_one_line_are_rejected(line):
    match = sites_of(_body(line), 3)
    assert problem_kinds(match) == [SiteProblemKind.SEVERAL_LOOPS]
    assert "2 loop keywords" in match.problems[0].describe()


def test_keywords_are_counted_per_origin_line_not_per_output_line():
    match = sites_of(SAME_ORIGIN_ON_TWO_LINES, 3, source=_body("while (n) { n--; } for (;;) { }"))
    assert problem_kinds(match) == [SiteProblemKind.SEVERAL_LOOPS]


@pytest.mark.parametrize(
    ("body", "line", "word"),
    [
        (_body("do {", "    n--;", "} while (n > 0);"), 3, "do"),
        (_body("again:", "if (n-- > 0)", "    goto again;"), 5, "goto"),
    ],
)
def test_do_and_goto_loops_are_not_contractable(body, line, word):
    match = sites_of(body, line)
    assert problem_kinds(match) == [SiteProblemKind.NOT_CONTRACTABLE]
    assert f"a {word} loop cannot take a contract" in match.problems[0].message


@pytest.mark.parametrize(
    "spelled",
    ["REPEAT(i) {", "for (int i = 0; i < n; i++) {", "SKIP(while) while (n) {"],
)
def test_a_keyword_the_source_line_does_not_spell_exactly_once_is_a_macro_loop(spelled):
    body = _body("while (n) {", "    n--;", "}")
    source = _body(spelled, "    n--;", "}")
    assert only_problem_kind(body, 3, source=source) is SiteProblemKind.MACRO_LOOP


def test_the_spelling_check_reads_the_source_line_of_the_contract():
    body = _body("while (n) {", "    n--;", "}")
    source = _body("n++;", "while (n) {", "}")
    assert only_problem_kind(body, 3, source=source) is SiteProblemKind.MACRO_LOOP


@pytest.mark.parametrize("line", ["while n {", "while (n {", "for", "while [n] {"])
def test_a_keyword_without_a_balanced_header_is_rejected(line):
    assert only_problem_kind(f"void f(int n)\n{{\n    {line}\n", 3) is SiteProblemKind.NO_HEADER


def test_a_header_that_ends_in_another_file_is_rejected():
    assert only_problem_kind(HEADER_IN_OTHER_FILE, 3) is SiteProblemKind.HEADER_OUTSIDE_FILE


@pytest.mark.parametrize(
    ("body", "line"),
    [
        (_body("do {", "    n--;", "} while (n > 0);"), 5),
        (_body("while (n-- > 0);"), 3),
        (_body("for (;;)", "    ;"), 3),
    ],
)
def test_a_header_followed_by_a_semicolon_is_rejected(body, line):
    match = sites_of(body, line)
    assert problem_kinds(match) == [SiteProblemKind.EMPTY_BODY]
    assert "do-while" in match.problems[0].message


@pytest.mark.parametrize(
    "body",
    [_body("for (int i = 0; i < n; i++)", "    n--;"), "void f(int n)\n{\n    while (n)"],
)
def test_a_body_that_is_not_a_block_is_rejected(body):
    assert only_problem_kind(body, 3) is SiteProblemKind.BODY_NOT_BLOCK


def test_a_body_opened_by_a_digraph_is_rejected():
    assert only_problem_kind(_body("while (n) <% n--; %>"), 3) is SiteProblemKind.DIGRAPH_BRACE


def test_output_with_a_line_splice_cannot_be_mapped():
    match = sites_of(SPLICED, 5, 3)
    assert problem_kinds(match) == [SiteProblemKind.UNANALYZABLE, SiteProblemKind.UNANALYZABLE]
    assert [problem.location for problem in match.problems] == [f"{REL}:5", f"{REL}:3"]
    assert "line splice" in match.problems[0].describe()


def test_contracts_of_other_files_are_ignored():
    pp = preprocessed(SPLICED)
    assert loop_sites(REL, pp, SPLICED, [contract_at(5, file=OTHER_REL)]) == LoopSiteMatch()
    assert loop_sites(REL, pp, SPLICED, []) == LoopSiteMatch()


def test_paths_are_compared_after_normalisation():
    (matched,) = loop_sites("./" + REL, preprocessed(FOR_LOOP), FOR_LOOP, [contract_at(3)]).matched
    assert matched.site.file == REL
    match = loop_sites(REL, preprocessed(FOR_LOOP, rel="./" + REL), FOR_LOOP, [contract_at(3)])
    assert match.accepted and [m.site.file for m in match.matched] == [REL]


def test_matched_loops_and_problems_are_reported_together_in_contract_order():
    body = _body("for (int i = 0; i < n; i++) {", "}", "while (n) {", "    n--;", "}")
    match = sites_of(body, 5, 4, 3)
    assert [m.site.line for m in match.matched] == [5, 3]
    assert [(p.kind, p.line) for p in match.problems] == [(SiteProblemKind.NO_LOOP, 4)]
    assert not match.accepted


def test_a_problem_describes_its_location():
    match = sites_of(FOR_LOOP, 2)
    assert match.problems[0].describe() == f"{REL}:2: no for or while loop starts on this line"


def test_probe_prefixes():
    assert (FUNCTION_PROBE_PREFIX, LOOP_PROBE_PREFIX) == ("leanc_function:", "leanc_loop:")
