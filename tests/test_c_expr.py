from __future__ import annotations

import pytest
from loop_contract_support import CLAUSE_FIXTURES, MUTATION_FIXTURES, fixture_clauses

from leanc.lang.c_expr import (
    INTEGER_TYPE_SPELLINGS,
    INTEGER_TYPEDEF_NAMES,
    Binary,
    BinaryOperator,
    Cast,
    ClauseKind,
    Index,
    IntConstant,
    LoopEntry,
    Member,
    Name,
    ObjectUpto,
    ObjectWhole,
    Unary,
    UnaryOperator,
    parse_clause,
)
from leanc.lang.c_expr_lexer import (
    MAX_CLAUSE_CHARS,
    MAX_CLAUSE_DEPTH,
    MAX_CLAUSE_TOKENS,
    ClauseRejected,
    Rejection,
    tokenize,
)

INVARIANT, ASSIGNS, DECREASES = ClauseKind.INVARIANT, ClauseKind.ASSIGNS, ClauseKind.DECREASES
DANGEROUS_RENDERED_SEQUENCES = ("++", "--", "//", "/*", "<:", ":>", "<%", "%>", "%:", "\n", ";", "{", "?")

ACCEPTED: list[tuple[ClauseKind, str, str]] = [
    (INVARIANT, "x", "x"),
    (INVARIANT, "0", "0"),
    (INVARIANT, "0 <= lo && lo <= hi && hi <= len", "(((0 <= lo) && (lo <= hi)) && (hi <= len))"),
    (INVARIANT, "a || b && c", "(a || (b && c))"),
    (INVARIANT, "a && b | c", "(a && (b | c))"),
    (INVARIANT, "a | b ^ c & d", "(a | (b ^ (c & d)))"),
    (INVARIANT, "a & b == c", "(a & (b == c))"),
    (INVARIANT, "a == b < c", "(a == (b < c))"),
    (INVARIANT, "a != b >= c", "(a != (b >= c))"),
    (INVARIANT, "a < b << c", "(a < (b << c))"),
    (INVARIANT, "a > b >> c", "(a > (b >> c))"),
    (INVARIANT, "a << b + c", "(a << (b + c))"),
    (INVARIANT, "a + b * c", "(a + (b * c))"),
    (INVARIANT, "a - b / c % d", "(a - ((b / c) % d))"),
    (INVARIANT, "a - b - c", "((a - b) - c)"),
    (INVARIANT, "a < b < c", "((a < b) < c)"),
    (INVARIANT, "(a + b) * c", "((a + b) * c)"),
    (INVARIANT, "-a * b", "((-a) * b)"),
    (INVARIANT, "!a == b", "((!a) == b)"),
    (INVARIANT, "~-x", "(~(-x))"),
    (INVARIANT, "a - -b", "(a - (-b))"),
    (INVARIANT, "- - a", "(-(-a))"),
    (INVARIANT, "!!a", "(!(!a))"),
    (INVARIANT, "*p", "(*p)"),
    (INVARIANT, "**p", "(*(*p))"),
    (INVARIANT, "*p[i]", "(*p[i])"),
    (INVARIANT, "(*p)[i]", "(*p)[i]"),
    (INVARIANT, "s->items[k]", "s->items[k]"),
    (INVARIANT, "s.a.b", "s.a.b"),
    (INVARIANT, "(*s).f", "(*s).f"),
    (INVARIANT, "s->next->len", "s->next->len"),
    (INVARIANT, "a[b[i]]", "a[b[i]]"),
    (INVARIANT, "(p + 1)->f", "(p + 1)->f"),
    (INVARIANT, "(int)a + b", "(((int)a) + b)"),
    (INVARIANT, "(long)(i) <= (long)(n)", "(((long)i) <= ((long)n))"),
    (INVARIANT, "(size_t)-1", "((size_t)(-1))"),
    (INVARIANT, "(size_t)a[0]", "((size_t)a[0])"),
    (INVARIANT, "(unsigned char)(int)x", "((unsigned char)((int)x))"),
    (INVARIANT, "(unsigned long long int)x", "((unsigned long long int)x)"),
    (INVARIANT, "0x1UL + 07 + 10u", "((0x1UL + 07) + 10u)"),
    (INVARIANT, "9223372036854775807", "9223372036854775807"),
    (INVARIANT, "18446744073709551615u", "18446744073709551615u"),
    (INVARIANT, "0xFFFFFFFFFFFFFFFF", "0xFFFFFFFFFFFFFFFF"),
    (INVARIANT, "0777LL + 1lu + 2Ul + 3llu", "(((0777LL + 1lu) + 2Ul) + 3llu)"),
    (INVARIANT, "__CPROVER_loop_entry(i) == 0", "(__CPROVER_loop_entry(i) == 0)"),
    (INVARIANT, "k == __CPROVER_loop_entry(s->fill) - i", "(k == (__CPROVER_loop_entry(s->fill) - i))"),
    (
        INVARIANT,
        "__CPROVER_loop_entry(*p) <= __CPROVER_loop_entry(a[0])",
        "(__CPROVER_loop_entry((*p)) <= __CPROVER_loop_entry(a[0]))",
    ),
    (INVARIANT, "  a   <=   b  ", "(a <= b)"),
    (INVARIANT, "x_1 && _lower && main", "((x_1 && _lower) && main)"),
    (DECREASES, "hi - lo", "(hi - lo)"),
    (DECREASES, "144 - k", "(144 - k)"),
    (DECREASES, "len", "len"),
    (ASSIGNS, "lo", "lo"),
    (ASSIGNS, "s->items[k]", "s->items[k]"),
    (ASSIGNS, "*p", "(*p)"),
    (ASSIGNS, "*(p + 1)", "(*(p + 1))"),
    (ASSIGNS, "(*s).f", "(*s).f"),
    (ASSIGNS, "(lo)", "lo"),
    (ASSIGNS, "__CPROVER_object_whole(s->items)", "__CPROVER_object_whole(s->items)"),
    (ASSIGNS, "__CPROVER_object_upto(s->acc, 28)", "__CPROVER_object_upto(s->acc, 28)"),
    (ASSIGNS, "__CPROVER_object_upto(p + 1, n * 4)", "__CPROVER_object_upto((p + 1), (n * 4))"),
]


@pytest.mark.parametrize(("kind", "text", "rendered"), ACCEPTED)
def test_accepted_clause_renders_fully_parenthesised(kind, text, rendered):
    assert parse_clause(text, kind).render() == rendered


@pytest.mark.parametrize(("kind", "text", "rendered"), ACCEPTED)
def test_rendering_round_trips_to_the_same_tree(kind, text, rendered):
    tree = parse_clause(text, kind)
    again = parse_clause(tree.render(), kind)
    assert again == tree
    assert again.render() == rendered


@pytest.mark.parametrize(("kind", "text", "rendered"), ACCEPTED)
def test_rendered_text_is_one_line_without_comment_digraph_or_increment(kind, text, rendered):
    assert not any(sequence in rendered for sequence in DANGEROUS_RENDERED_SEQUENCES)
    assert all(" " <= character <= "~" for character in rendered)
    assert tokenize(rendered)


def test_parsed_trees_are_frozen_dataclasses():
    tree = parse_clause("s->a[i] + (int)*p", INVARIANT)
    assert tree == Binary(
        BinaryOperator.ADD,
        Index(Member(Name("s"), "a", through_pointer=True), Name("i")),
        Cast("int", Unary(UnaryOperator.DEREFERENCE, Name("p"))),
    )
    with pytest.raises(AttributeError):
        tree.left = Name("q")
    assert hash(tree) == hash(parse_clause(tree.render(), INVARIANT))


def test_special_forms_build_their_own_nodes():
    assert parse_clause("__CPROVER_loop_entry(i)", INVARIANT) == LoopEntry(Name("i"))
    assert parse_clause("__CPROVER_object_whole(p)", ASSIGNS) == ObjectWhole(Name("p"))
    assert parse_clause("__CPROVER_object_upto(p, 4)", ASSIGNS) == ObjectUpto(Name("p"), IntConstant("4"))


@pytest.mark.parametrize("spelling", sorted(INTEGER_TYPE_SPELLINGS | INTEGER_TYPEDEF_NAMES))
def test_every_plain_integer_type_is_a_cast_target(spelling):
    assert parse_clause(f"({spelling})x", INVARIANT).render() == f"(({spelling})x)"


@pytest.mark.parametrize(
    "spelling", ["0", "7", "42", "0x2a", "0X2A", "052", "1u", "1U", "1l", "1L", "1ll", "1LL", "1ul", "1LLU", "1lU"]
)
def test_integer_constant_spellings_are_kept(spelling):
    assert parse_clause(spelling, INVARIANT) == IntConstant(spelling)


def _wide_clause_with_exactly_the_token_limit() -> str:
    group = "(" + "+".join(["a"] * 15) + ")"
    return "-" + "&&".join([group] * 8)


def test_limits_are_inclusive():
    nested = "(" * MAX_CLAUSE_DEPTH + "a" + ")" * MAX_CLAUSE_DEPTH
    assert parse_clause(nested, INVARIANT) == Name("a")
    chain = " && ".join(["a"] * MAX_CLAUSE_DEPTH)
    assert parse_clause(chain, INVARIANT).height() == MAX_CLAUSE_DEPTH
    wide = _wide_clause_with_exactly_the_token_limit()
    assert len(tokenize(wide)) == MAX_CLAUSE_TOKENS
    assert parse_clause(wide, INVARIANT).height() <= MAX_CLAUSE_DEPTH
    longest = "a" * MAX_CLAUSE_CHARS
    assert parse_clause(longest, INVARIANT) == Name(longest)


def test_deep_input_never_overflows_the_python_stack():
    for text in ("(" * 250 + "a" + ")" * 250, "!" * 255 + "x", "~" * 255 + "x", "(int)" * 99 + "x", "(int)" * 60 + "x"):
        with pytest.raises(ClauseRejected) as rejected:
            parse_clause(text, INVARIANT)
        assert rejected.value.rejection in {Rejection.TOO_DEEP, Rejection.TOO_LONG, Rejection.TOO_MANY_TOKENS}


def test_worst_case_parser_recursion_within_the_input_limits_is_rejected_cleanly():
    level = "a||b&&c|d^e&f==g<h<<i+j*("
    tokens_per_level = len(tokenize(level + "z)")) - 1
    depth = min((MAX_CLAUSE_CHARS - 1) // (len(level) + 1), (MAX_CLAUSE_TOKENS - 1) // tokens_per_level)
    text = level * depth + "z" + ")" * depth
    assert len(text) <= MAX_CLAUSE_CHARS and len(tokenize(text)) <= MAX_CLAUSE_TOKENS
    with pytest.raises(ClauseRejected) as rejected:
        parse_clause(text, INVARIANT)
    assert rejected.value.rejection is Rejection.TOO_DEEP


BENIGN_MUTATIONS = (
    "ok.c",
    "ok_buggy_code.c",
    "weak_invariant.c",
    "false_at_entry.c",
    "not_preserved.c",
    "invariant_false.c",
    "invariant_contradiction_exit.c",
    "wrong_decreases.c",
    "decreases_nonterminating.c",
    "assigns_too_small.c",
    "write_outside_assigns.c",
)


@pytest.mark.parametrize("name", BENIGN_MUTATIONS)
def test_side_effect_free_mutation_clauses_are_left_to_the_tool(name):
    clauses = fixture_clauses(MUTATION_FIXTURES / name)
    assert set(clauses) == set(ClauseKind)
    for kind, texts in clauses.items():
        for text in texts:
            parse_clause(text, kind)


@pytest.mark.parametrize("name", ["c1.c", "c10.c", "c11.c"])
def test_measured_clause_forms_are_accepted(name):
    for kind, texts in fixture_clauses(CLAUSE_FIXTURES / name).items():
        for text in texts:
            parse_clause(text, kind)
