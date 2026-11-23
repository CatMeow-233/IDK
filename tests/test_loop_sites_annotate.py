from __future__ import annotations

import pytest
from loop_site_support import REL, contract_at, preprocessed

from leanc.lang.c_lexer import CSource
from leanc.rules.definitions import DefinitionSite, defined_functions, function_definition_sites
from leanc.rules.loop_sites import (
    FunctionEntries,
    FunctionEntry,
    Insertion,
    Origin,
    SiteProblem,
    SiteProblemKind,
    annotate,
    contract_insertion,
    function_entries,
    function_probe_insertion,
    function_probe_label,
    loop_probe_insertion,
    loop_probe_label,
    loop_sites,
    origin_lines,
    probe_statement,
)
from leanc.rules.preprocessor import Linemarker, expanded_view, linemarker

LOOP = """int f(int n)
{
    while (n > 0) {
        n--;
    }
    return n;
}
"""
DEFINITIONS = """# 1 "include/h.h" 1
static inline int helper(int x) { return x; }
# 2 "src/loops.c" 2
int declared(int x);
int f(int n)
{
    return n;
}
static int g(void) <% return 0; %>
int h(void) { return 1; }
"""
MARKERS = 'before\n# 5 "./src/a.c" 1 3\nx\ny\n# 2 "<built-in>"\nz\n# 7 "src/we\\"ird.c"\nw'
SPLICED = "int f(void)\n{\n    return 1 + \\\n        2;\n}\n"


def _site_insertions(pp: str):
    (matched,) = loop_sites(REL, pp, LOOP, [contract_at(3)]).matched
    return matched, contract_insertion(matched), loop_probe_insertion(matched)


def test_linemarker_parses_line_and_unescaped_file():
    assert linemarker('# 12 "src/a.c" 2 3') == Linemarker(12, "src/a.c")
    assert linemarker('# 7 "src/we\\"ird.c"') == Linemarker(7, 'src/we"ird.c')
    assert linemarker("int x;") is None
    assert linemarker("#pragma once") is None


def test_origin_lines_follow_linemarkers():
    assert origin_lines(MARKERS) == [
        None,
        None,
        Origin("src/a.c", 5),
        Origin("src/a.c", 6),
        None,
        Origin("<built-in>", 2),
        None,
        Origin('src/we"ird.c', 7),
    ]


@pytest.mark.parametrize("rel", ["src/a.c", "<built-in>", "src/b.c"])
def test_expanded_view_is_the_projection_of_origin_lines(rel):
    lines = MARKERS.split("\n")
    projected = [(text, o.line) for text, o in zip(lines, origin_lines(MARKERS)) if o is not None and o.file == rel]
    assert expanded_view(rel, MARKERS) == ("\n".join(t for t, _ in projected), [line for _, line in projected])


def test_function_definition_sites_point_at_the_body_brace():
    src = CSource("int f(void) { return 0; }\nint g(int);\nstatic int h(int x) __attribute__((unused)) { return x; }\n")
    sites = function_definition_sites(src)
    assert [site.name for site in sites] == ["f", "h"]
    assert all(src.code_nodirectives[site.body_open] == "{" for site in sites)
    assert sites[0] == DefinitionSite("f", 4, 12)
    assert defined_functions(src) == [(site.name, site.offset) for site in sites]


def test_function_entries_are_the_wanted_definitions_of_the_file():
    pp = preprocessed(DEFINITIONS)
    found = function_entries(REL, pp, ["helper", "declared", "f", "g"])
    (entry,) = found.entries
    assert entry == FunctionEntry("f", REL, 3, pp.index("{\n    return n;"))
    assert pp[entry.body_offset - 1] == "{" and entry.body_offset == entry.body_open + 1
    assert found.problems == (
        SiteProblem(
            SiteProblemKind.DIGRAPH_BRACE, REL, 7, "the body of g opens with the digraph <%; write {"
        ),
    )


def test_function_entries_of_unwanted_names_are_not_reported():
    assert function_entries(REL, preprocessed(DEFINITIONS), ["h", "missing"]).entries[0].name == "h"
    assert function_entries(REL, preprocessed(DEFINITIONS), []) == FunctionEntries()


def test_function_entries_need_mappable_offsets():
    found = function_entries(REL, preprocessed(SPLICED), ["f"])
    assert not found.entries
    (problem,) = found.problems
    assert (problem.kind, problem.location) == (SiteProblemKind.UNANALYZABLE, REL)
    assert problem.describe().startswith(f"{REL}: the cbmc --preprocess output of {REL} still has a line splice")


def test_probe_labels():
    assert function_probe_label("iset_rank") == "leanc_function:iset_rank"
    assert loop_probe_label("src/iset.c", 11) == "leanc_loop:src/iset.c:11"


@pytest.mark.parametrize(
    ("label", "literal"),
    [
        ("leanc_function:f", '"leanc_function:f"'),
        ('a"b\\c?d', '"a\\"b\\\\c\\?d"'),
        (" ~", '" ~"'),
        ("\t\x7f", '"\\011\\177"'),
        ("é", '"\\303\\251"'),
        ("\udcff", '"\\377"'),
    ],
)
def test_probe_statement_renders_one_line_of_c(label, literal):
    assert probe_statement(label) == f" __CPROVER_assert(0, {literal}); "


def test_annotate_applies_insertions_in_any_given_order():
    insertions = [Insertion(1, "X"), Insertion(4, "Y"), Insertion(0, "<"), Insertion(6, ">")]
    assert annotate("abcdef", insertions) == "<aXbcdYef>"
    assert annotate("abcdef", reversed(insertions)) == "<aXbcdYef>"
    assert annotate("abcdef", []) == "abcdef"


@pytest.mark.parametrize(
    ("insertions", "message"),
    [
        ([Insertion(-1, "x")], "outside the text"),
        ([Insertion(7, "x")], "outside the text"),
        ([Insertion(2, "x"), Insertion(2, "y")], "two insertions at offset 2"),
        ([Insertion(2, "x\ny")], "line break"),
        ([Insertion(2, "x\ry")], "line break"),
    ],
)
def test_annotate_rejects_unsafe_insertions(insertions, message):
    with pytest.raises(ValueError, match=message):
        annotate("abcdef", insertions)


def test_the_contract_goes_after_the_header_and_the_probe_after_the_body_brace():
    pp = preprocessed(LOOP)
    matched, clauses, probe = _site_insertions(pp)
    assert clauses == Insertion(matched.site.header_close + 1, matched.contract.annotation())
    assert probe == Insertion(matched.site.body_open + 1, probe_statement("leanc_loop:src/loops.c:3"))
    annotated = annotate(pp, [probe, clauses])
    expected_line = (
        f"    while (n > 0){matched.contract.annotation()} {{{probe_statement('leanc_loop:src/loops.c:3')}"
    )
    assert annotated.split("\n")[3] == expected_line
    assert annotated.count("\n") == pp.count("\n")
    assert len(origin_lines(annotated)) == len(origin_lines(pp))


def test_the_function_probe_goes_after_the_body_brace():
    pp = preprocessed(LOOP)
    (entry,) = function_entries(REL, pp, ["f"]).entries
    insertion = function_probe_insertion(entry)
    assert insertion == Insertion(entry.body_open + 1, probe_statement("leanc_function:f"))
    assert annotate(pp, [insertion]).split("\n")[2] == "{" + probe_statement("leanc_function:f")
