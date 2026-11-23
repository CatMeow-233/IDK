from __future__ import annotations

import pytest
from loop_contract_support import CLAUSE_FIXTURES, MUTATION_FIXTURES, breakout_payload, fixture_clauses

from leanc.lang.c_expr import ClauseKind, parse_clause
from leanc.lang.c_expr_lexer import MAX_CLAUSE_CHARS, MAX_CLAUSE_DEPTH, MAX_CLAUSE_TOKENS, ClauseRejected, Rejection

INVARIANT, ASSIGNS, DECREASES = ClauseKind.INVARIANT, ClauseKind.ASSIGNS, ClauseKind.DECREASES

REJECTED: list[tuple[ClauseKind, str, Rejection]] = [
    (INVARIANT, "", Rejection.EMPTY),
    (INVARIANT, "   ", Rejection.EMPTY),
    (INVARIANT, "a" * (MAX_CLAUSE_CHARS + 1), Rejection.TOO_LONG),
    (INVARIANT, "+".join(["a"] * (MAX_CLAUSE_TOKENS // 2 + 1)), Rejection.TOO_MANY_TOKENS),
    (INVARIANT, "(" * (MAX_CLAUSE_DEPTH + 1) + "a" + ")" * (MAX_CLAUSE_DEPTH + 1), Rejection.TOO_DEEP),
    (INVARIANT, "&&".join(["a"] * (MAX_CLAUSE_DEPTH + 1)), Rejection.TOO_DEEP),
    (INVARIANT, "!" * MAX_CLAUSE_DEPTH + "a", Rejection.TOO_DEEP),
    (INVARIANT, "a\nb", Rejection.CHARACTER),
    (INVARIANT, "a\tb", Rejection.CHARACTER),
    (INVARIANT, "a\x00", Rejection.CHARACTER),
    (INVARIANT, "{\n}", Rejection.CHARACTER),
    (INVARIANT, "\u202e__CPROVER_forall", Rejection.CHARACTER),
    (INVARIANT, "a ‮ b", Rejection.CHARACTER),
    (INVARIANT, "é == 1", Rejection.CHARACTER),
    (INVARIANT, "a; b", Rejection.CHARACTER),
    (INVARIANT, '"s"', Rejection.CHARACTER),
    (INVARIANT, "'c' == x", Rejection.CHARACTER),
    (INVARIANT, "#a", Rejection.CHARACTER),
    (INVARIANT, "a \\ b", Rejection.CHARACTER),
    (INVARIANT, "a$b", Rejection.CHARACTER),
    (INVARIANT, "a @ b", Rejection.CHARACTER),
    (INVARIANT, "`a`", Rejection.CHARACTER),
    (INVARIANT, "__CPROVER_forall { int q; q == q }", Rejection.QUANTIFIER),
    (INVARIANT, "a && __CPROVER_exists", Rejection.QUANTIFIER),
    (INVARIANT, "({ __CPROVER_assume(0); 1; })", Rejection.BRACE),
    (INVARIANT, "((struct pair){ 0 }).b == 0", Rejection.BRACE),
    (INVARIANT, "a } b", Rejection.BRACE),
    (INVARIANT, "a /* c */", Rejection.COMMENT),
    (INVARIANT, "a // c", Rejection.COMMENT),
    (INVARIANT, "a/*p", Rejection.COMMENT),
    (INVARIANT, "a <: 1 :>", Rejection.DIGRAPH),
    (INVARIANT, "a <% b", Rejection.DIGRAPH),
    (INVARIANT, "a %> b", Rejection.DIGRAPH),
    (INVARIANT, "%:define x", Rejection.DIGRAPH),
    (INVARIANT, "lo++ >= 0", Rejection.INCREMENT),
    (INVARIANT, "--lo >= 0", Rejection.INCREMENT),
    (INVARIANT, "a--b", Rejection.INCREMENT),
    (INVARIANT, "i > 0 ? 1 : 0", Rejection.CONDITIONAL),
    (INVARIANT, "a ?: b", Rejection.CONDITIONAL),
    (INVARIANT, "a ??= b", Rejection.CONDITIONAL),
    (INVARIANT, "(hi = lo) >= 0", Rejection.ASSIGNMENT),
    (INVARIANT, "(g += 0, 1)", Rejection.ASSIGNMENT),
    (INVARIANT, "a -= 1", Rejection.ASSIGNMENT),
    (INVARIANT, "a *= 1", Rejection.ASSIGNMENT),
    (INVARIANT, "a /= 1", Rejection.ASSIGNMENT),
    (INVARIANT, "a %= 1", Rejection.ASSIGNMENT),
    (INVARIANT, "a &= 1", Rejection.ASSIGNMENT),
    (INVARIANT, "a |= 1", Rejection.ASSIGNMENT),
    (INVARIANT, "a ^= 1", Rejection.ASSIGNMENT),
    (INVARIANT, "a <<= 1", Rejection.ASSIGNMENT),
    (INVARIANT, "a >>= 1", Rejection.ASSIGNMENT),
    (INVARIANT, "a !== 1", Rejection.ASSIGNMENT),
    (INVARIANT, "1.5 > a", Rejection.CONSTANT),
    (INVARIANT, "1.f", Rejection.CONSTANT),
    (INVARIANT, ".5 > a", Rejection.CONSTANT),
    (INVARIANT, "1e5", Rejection.CONSTANT),
    (INVARIANT, "0b101", Rejection.CONSTANT),
    (INVARIANT, "08", Rejection.CONSTANT),
    (INVARIANT, "0x", Rejection.CONSTANT),
    (INVARIANT, "1lL", Rejection.CONSTANT),
    (INVARIANT, "1uu", Rejection.CONSTANT),
    (INVARIANT, "123abc", Rejection.CONSTANT),
    (INVARIANT, "9223372036854775808", Rejection.CONSTANT),
    (INVARIANT, "18446744073709551616u", Rejection.CONSTANT),
    (INVARIANT, "0x10000000000000000", Rejection.CONSTANT),
    (INVARIANT, "1 .f", Rejection.CONSTANT),
    (INVARIANT, "(1)[p]", Rejection.CONSTANT),
    (INVARIANT, "hi - lo) if (0) while (lo < hi", Rejection.UNBALANCED),
    (INVARIANT, "(a", Rejection.UNBALANCED),
    (INVARIANT, "a]", Rejection.UNBALANCED),
    (INVARIANT, "(a]", Rejection.UNBALANCED),
    (INVARIANT, "a, b", Rejection.COMMA),
    (INVARIANT, "a[1, 2]", Rejection.COMMA),
    (INVARIANT, "(a, b)", Rejection.COMMA),
    (ASSIGNS, "lo, hi", Rejection.COMMA),
    (ASSIGNS, "__CPROVER_object_whole(p, n)", Rejection.COMMA),
    (ASSIGNS, "__CPROVER_object_upto(p, n, m)", Rejection.COMMA),
    (INVARIANT, "&x == p", Rejection.ADDRESS_OF),
    (INVARIANT, "a & &b", Rejection.ADDRESS_OF),
    (ASSIGNS, "*&x", Rejection.ADDRESS_OF),
    (INVARIANT, "abs(lo) >= 0", Rejection.CALL),
    (INVARIANT, "fp() >= 0", Rejection.CALL),
    (INVARIANT, "(fp)() >= 0", Rejection.CALL),
    (INVARIANT, "(*fp)() >= 0", Rejection.CALL),
    (INVARIANT, "a[0](x)", Rejection.CALL),
    (INVARIANT, "s->f()", Rejection.CALL),
    (INVARIANT, "s.f(1)", Rejection.CALL),
    (INVARIANT, "sizeof(g) > 0", Rejection.KEYWORD),
    (INVARIANT, "sizeof g", Rejection.KEYWORD),
    (INVARIANT, "int", Rejection.KEYWORD),
    (INVARIANT, "(void *)0", Rejection.KEYWORD),
    (INVARIANT, "(struct x *)p", Rejection.KEYWORD),
    (INVARIANT, "(const int)x", Rejection.KEYWORD),
    (INVARIANT, "(float)x", Rejection.KEYWORD),
    (INVARIANT, "a->int", Rejection.KEYWORD),
    (INVARIANT, "__CPROVER_assume(0)", Rejection.RESERVED_IDENTIFIER),
    (INVARIANT, "__builtin_expect(i, 0)", Rejection.RESERVED_IDENTIFIER),
    (INVARIANT, "__CPROVER_r_ok(p, 4)", Rejection.RESERVED_IDENTIFIER),
    (INVARIANT, "_Bool", Rejection.RESERVED_IDENTIFIER),
    (INVARIANT, "_Alignof(x)", Rejection.RESERVED_IDENTIFIER),
    (INVARIANT, "(_Bool)x", Rejection.RESERVED_IDENTIFIER),
    (INVARIANT, "s.__pad", Rejection.RESERVED_IDENTIFIER),
    (INVARIANT, "nondet_int() > 0", Rejection.FORBIDDEN_PREFIX),
    (INVARIANT, "nondet_value", Rejection.FORBIDDEN_PREFIX),
    (INVARIANT, "LEANC_ASSUME", Rejection.FORBIDDEN_PREFIX),
    (INVARIANT, "leanc_inductive_memcpy", Rejection.FORBIDDEN_PREFIX),
    (INVARIANT, "s->leanc_hook", Rejection.FORBIDDEN_PREFIX),
    (INVARIANT, "p != NULL", Rejection.MACRO),
    (INVARIANT, "x < INT_MAX", Rejection.MACRO),
    (INVARIANT, "n <= SIZE_MAX", Rejection.MACRO),
    (INVARIANT, "b == true", Rejection.MACRO),
    (INVARIANT, "x <= UINT32_MAX", Rejection.MACRO),
    (INVARIANT, "size_t", Rejection.CAST),
    (INVARIANT, "uint32_t + 1", Rejection.CAST),
    (INVARIANT, "(unsigned char *)p", Rejection.CAST),
    (INVARIANT, "(int unsigned)x", Rejection.CAST),
    (INVARIANT, "(long long long)x", Rejection.CAST),
    (INVARIANT, "(unsigned size_t)x", Rejection.CAST),
    (INVARIANT, "(size_t + 1)", Rejection.CAST),
    (DECREASES, "__CPROVER_loop_entry(n) - i", Rejection.MISPLACED_FORM),
    (ASSIGNS, "__CPROVER_loop_entry(i)", Rejection.MISPLACED_FORM),
    (INVARIANT, "__CPROVER_loop_entry(__CPROVER_loop_entry(i))", Rejection.MISPLACED_FORM),
    (INVARIANT, "__CPROVER_object_whole(p)", Rejection.MISPLACED_FORM),
    (DECREASES, "__CPROVER_object_upto(p, 4)", Rejection.MISPLACED_FORM),
    (ASSIGNS, "g[__CPROVER_object_whole(p)]", Rejection.MISPLACED_FORM),
    (ASSIGNS, "__CPROVER_object_whole(__CPROVER_object_whole(p))", Rejection.MISPLACED_FORM),
    (ASSIGNS, "__CPROVER_object_upto(p, __CPROVER_loop_entry(n))", Rejection.MISPLACED_FORM),
    (INVARIANT, "__CPROVER_loop_entry(i + 1)", Rejection.NOT_LVALUE),
    (INVARIANT, "__CPROVER_loop_entry(-i)", Rejection.NOT_LVALUE),
    (ASSIGNS, "a + b", Rejection.NOT_LVALUE),
    (ASSIGNS, "(int)x", Rejection.NOT_LVALUE),
    (ASSIGNS, "-x", Rejection.NOT_LVALUE),
    (ASSIGNS, "0", Rejection.NOT_LVALUE),
    (INVARIANT, "+x", Rejection.SYNTAX),
    (INVARIANT, "a +", Rejection.SYNTAX),
    (INVARIANT, "a b", Rejection.SYNTAX),
    (INVARIANT, "()", Rejection.SYNTAX),
    (INVARIANT, "a. 1", Rejection.SYNTAX),
    (INVARIANT, "a.1", Rejection.CONSTANT),
    (INVARIANT, "a->", Rejection.SYNTAX),
    (INVARIANT, "a[]", Rejection.SYNTAX),
    (INVARIANT, "__CPROVER_loop_entry + 1", Rejection.SYNTAX),
    (ASSIGNS, "__CPROVER_object_upto(p)", Rejection.SYNTAX),
    (ASSIGNS, "__CPROVER_object_whole(p) + 1", Rejection.SYNTAX),
]


@pytest.mark.parametrize(("kind", "text", "rejection"), REJECTED)
def test_rejected_clause_names_its_reason(kind, text, rejection):
    with pytest.raises(ClauseRejected) as rejected:
        parse_clause(text, kind)
    assert rejected.value.rejection is rejection
    assert str(rejected.value).startswith(rejection.value + ": ")


def test_every_rejection_reason_has_a_case():
    assert {rejection for _, _, rejection in REJECTED} == set(Rejection)


def test_project_macro_names_are_rejected_only_when_supplied():
    assert parse_clause("n <= LIMIT", INVARIANT).render() == "(n <= LIMIT)"
    with pytest.raises(ClauseRejected) as rejected:
        parse_clause("n <= LIMIT", INVARIANT, macro_names=frozenset({"LIMIT"}))
    assert rejected.value.rejection is Rejection.MACRO
    with pytest.raises(ClauseRejected) as member:
        parse_clause("s->LIMIT", ASSIGNS, macro_names=frozenset({"LIMIT"}))
    assert member.value.rejection is Rejection.MACRO


SIDE_EFFECTING_MUTATIONS: dict[str, tuple[ClauseKind, Rejection, frozenset[str]]] = {
    "assigns_assignment.c": (ASSIGNS, Rejection.ASSIGNMENT, frozenset()),
    "assigns_stmt_expr.c": (ASSIGNS, Rejection.BRACE, frozenset()),
    "assume_like_invariant_forall.c": (INVARIANT, Rejection.QUANTIFIER, frozenset()),
    "decreases_assignment.c": (DECREASES, Rejection.ASSIGNMENT, frozenset()),
    "decreases_stmt_expr.c": (DECREASES, Rejection.BRACE, frozenset()),
    "invariant_assignment.c": (INVARIANT, Rejection.ASSIGNMENT, frozenset()),
    "invariant_call.c": (INVARIANT, Rejection.CALL, frozenset()),
    "invariant_increment.c": (INVARIANT, Rejection.INCREMENT, frozenset()),
    "invariant_stmt_expr.c": (INVARIANT, Rejection.BRACE, frozenset()),
    "macro_assignment_in_invariant.c": (INVARIANT, Rejection.MACRO, frozenset({"LIMIT"})),
    "macro_assume_in_invariant.c": (INVARIANT, Rejection.MACRO, frozenset({"OK"})),
}


def _rejection_of(text: str, kind: ClauseKind, macro_names: frozenset[str]) -> Rejection | None:
    try:
        parse_clause(text, kind, macro_names=macro_names)
    except ClauseRejected as exc:
        return exc.rejection
    return None


@pytest.mark.parametrize("name", sorted(SIDE_EFFECTING_MUTATIONS))
def test_side_effecting_clauses_of_the_measured_mutations_are_rejected(name):
    kind, rejection, macro_names = SIDE_EFFECTING_MUTATIONS[name]
    clauses = fixture_clauses(MUTATION_FIXTURES / name)
    outcomes = [(k, _rejection_of(text, k, macro_names)) for k, texts in clauses.items() for text in texts]
    assert [(k, outcome) for k, outcome in outcomes if outcome is not None] == [(kind, rejection)]


def test_the_measured_breakout_payload_cannot_close_the_clause():
    payload = breakout_payload(MUTATION_FIXTURES / "breakout.c")
    assert payload.startswith("hi - lo)") and "while (lo < hi" in payload
    assert _rejection_of(payload, DECREASES, frozenset()) is Rejection.BRACE
    without_block = payload.replace(" { break; }", "")
    assert _rejection_of(without_block, DECREASES, frozenset()) is Rejection.UNBALANCED


MEASURED_CLAUSE_ATTACKS: dict[str, Rejection] = {
    "c2.c": Rejection.CALL,
    "c3.c": Rejection.CALL,
    "c4.c": Rejection.CALL,
    "c5.c": Rejection.BRACE,
    "c6.c": Rejection.CONDITIONAL,
    "c7.c": Rejection.ASSIGNMENT,
    "c8.c": Rejection.RESERVED_IDENTIFIER,
    "c9.c": Rejection.ASSIGNMENT,
    "c12.c": Rejection.QUANTIFIER,
    "c13.c": Rejection.RESERVED_IDENTIFIER,
    "c14.c": Rejection.RESERVED_IDENTIFIER,
    "c15.c": Rejection.RESERVED_IDENTIFIER,
    "c16.c": Rejection.RESERVED_IDENTIFIER,
}


@pytest.mark.parametrize("name", sorted(MEASURED_CLAUSE_ATTACKS))
def test_measured_clause_attacks_are_rejected(name):
    clauses = fixture_clauses(CLAUSE_FIXTURES / name)
    assert _rejection_of(clauses[INVARIANT][0], INVARIANT, frozenset()) is MEASURED_CLAUSE_ATTACKS[name]
    assert all(_rejection_of(text, ASSIGNS, frozenset()) is None for text in clauses[ASSIGNS])
    assert _rejection_of(clauses[DECREASES][0], DECREASES, frozenset()) is None
