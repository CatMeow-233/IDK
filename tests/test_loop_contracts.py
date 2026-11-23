from __future__ import annotations

import json

import pytest
from loop_contract_support import (
    ACCEPTANCE_FIXTURES,
    INDUCTIVE_FIXTURES,
    LOOP_LINE,
    SECOND_SOURCE,
    contracts_text,
    inductive_spec,
    loop_entry,
)
from spec_inductive_support import SAFE_ARRAY_SOURCE

from leanc.lang.c_expr import Name
from leanc.lang.c_expr_lexer import Rejection
from leanc.rules.loop_contracts import (
    MAX_CLAUSES_PER_LIST,
    MAX_CONTRACTED_LOOPS,
    MAX_LOOP_CONTRACTS_CHARS,
    MAX_LOOP_LINE,
    ContractProblemKind,
    LoopContracts,
    is_loop_contract_path,
    parse_loop_contracts,
)

ENTRY_KEYS = ("file", "line", "invariant", "assigns", "decreases")
OLD_SCHEMA_FIXTURES = (
    ACCEPTANCE_FIXTURES / "iset" / "contracts.json",
    ACCEPTANCE_FIXTURES / "sponge" / "contracts.json",
    ACCEPTANCE_FIXTURES / "attrtab" / "contracts.json",
    INDUCTIVE_FIXTURES / "soundness-e4" / "contracts.json",
    INDUCTIVE_FIXTURES / "soundness-e4" / "scontracts.json",
)


@pytest.fixture(scope="module")
def spec():
    return inductive_spec([SAFE_ARRAY_SOURCE, SECOND_SOURCE])


def _problems(spec, text: str, **kwargs) -> list[str]:
    contracts = parse_loop_contracts(spec, text, **kwargs)
    assert not contracts.loops
    assert not contracts.accepted
    return [problem.describe() for problem in contracts.problems]


def test_a_missing_contracts_file_means_no_contracts(spec):
    assert parse_loop_contracts(spec, None) == LoopContracts()
    assert parse_loop_contracts(spec, None).accepted


def test_an_empty_loop_list_is_accepted(spec):
    assert parse_loop_contracts(spec, contracts_text()) == LoopContracts()


def test_a_valid_contract_is_parsed_into_trees_and_rendered_as_one_annotation(spec):
    text = contracts_text(
        loop_entry(),
        loop_entry(
            file=SECOND_SOURCE,
            invariant=["k <= 144", "s->fill < 144"],
            assigns=["k", "__CPROVER_object_upto(s->acc, 28)"],
            decreases="144 - k",
        ),
    )
    contracts = parse_loop_contracts(spec, text)
    assert contracts.accepted and len(contracts.loops) == 2
    first, second = contracts.loops
    assert (first.file, first.line) == (SAFE_ARRAY_SOURCE, LOOP_LINE)
    assert first.location == f"{SAFE_ARRAY_SOURCE}:{LOOP_LINE}"
    assert first.assigns == (Name("lo"), Name("hi"))
    assert first.annotation() == (
        " __CPROVER_assigns(lo, hi) __CPROVER_loop_invariant(((((0 <= lo) && (lo <= hi)) && (hi <= len))))"
        " __CPROVER_decreases((hi - lo)) "
    )
    assert second.annotation() == (
        " __CPROVER_assigns(k, __CPROVER_object_upto(s->acc, 28))"
        " __CPROVER_loop_invariant(((k <= 144)) && ((s->fill < 144))) __CPROVER_decreases((144 - k)) "
    )
    assert "\n" not in first.annotation() + second.annotation()


def test_the_contract_holds_no_raw_clause_text(spec):
    contracts = parse_loop_contracts(spec, contracts_text(loop_entry(invariant=["  lo   <=  hi  "])))
    loop = contracts.loops[0]
    assert "  lo" not in loop.annotation()
    assert all(not isinstance(value, str) for value in (*loop.invariant, *loop.assigns, loop.decreases))


@pytest.mark.parametrize("key", ENTRY_KEYS)
def test_every_entry_key_is_required(spec, key):
    entry = loop_entry()
    del entry[key]
    assert _problems(spec, contracts_text(entry)) == [f"loops.0.{key}: Field required"]


def test_a_contract_without_decreases_is_rejected(spec):
    entry = loop_entry()
    del entry["decreases"]
    problems = parse_loop_contracts(spec, contracts_text(entry)).problems
    assert [problem.kind for problem in problems] == [ContractProblemKind.DOCUMENT]
    assert "decreases" in problems[0].message


def test_the_loops_key_is_required(spec):
    assert _problems(spec, "{}") == ["loops: Field required"]


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"loops": [], "extra": 1}),
        contracts_text(loop_entry(function="lower_bound")),
        contracts_text(loop_entry(index=0)),
    ],
)
def test_extra_keys_are_rejected(spec, text):
    assert any("Extra inputs are not permitted" in problem for problem in _problems(spec, text))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("line", "11"),
        ("line", 11.0),
        ("line", True),
        ("line", None),
        ("line", 0),
        ("line", -1),
        ("line", MAX_LOOP_LINE + 1),
        ("file", 1),
        ("invariant", "0 <= lo"),
        ("invariant", []),
        ("invariant", ["1"] * (MAX_CLAUSES_PER_LIST + 1)),
        ("invariant", [1]),
        ("assigns", []),
        ("assigns", ["lo"] * (MAX_CLAUSES_PER_LIST + 1)),
        ("assigns", "lo"),
        ("decreases", ["hi - lo"]),
        ("decreases", 3),
    ],
)
def test_entry_values_have_strict_types_and_bounds(spec, field, value):
    problems = _problems(spec, contracts_text(loop_entry(**{field: value})))
    assert problems and all(problem.startswith(f"loops.0.{field}") for problem in problems)


def test_the_list_limits_are_inclusive(spec):
    entry = loop_entry(invariant=["1"] * MAX_CLAUSES_PER_LIST, assigns=["lo"] * MAX_CLAUSES_PER_LIST)
    assert parse_loop_contracts(spec, contracts_text(entry)).accepted


def test_the_line_limit_is_inclusive(spec):
    contracts = parse_loop_contracts(spec, contracts_text(loop_entry(line=MAX_LOOP_LINE)))
    assert contracts.accepted, contracts.problems
    assert [loop.line for loop in contracts.loops] == [MAX_LOOP_LINE]


def test_at_most_the_maximum_number_of_loops(spec):
    entries = [loop_entry(line=line) for line in range(1, MAX_CONTRACTED_LOOPS + 1)]
    assert len(parse_loop_contracts(spec, contracts_text(*entries)).loops) == MAX_CONTRACTED_LOOPS
    entries.append(loop_entry(line=MAX_CONTRACTED_LOOPS + 1))
    assert _problems(spec, contracts_text(*entries))[0].startswith("loops: List should have at most")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        '"loops"',
        "﻿" + contracts_text(),
        '{"loops": [], "loops": []}',
        '{"loops": [{"file": "src/safe_array.c", "file": "src/helper.c", "line": 1, "invariant": ["1"], '
        '"assigns": ["i"], "decreases": "n"}]}',
        "[" * 100_000 + "]" * 100_000,
        '{"loops": [{"file": "src/safe_array.c", "line": 1' + "0" * 5000 + ', "invariant": ["1"], '
        '"assigns": ["i"], "decreases": "n"}]}',
    ],
)
def test_malformed_documents_are_rejected(spec, text):
    problems = parse_loop_contracts(spec, text).problems
    assert problems and all(problem.kind is ContractProblemKind.DOCUMENT for problem in problems)


def test_duplicate_json_keys_are_reported_as_such(spec):
    assert _problems(spec, '{"loops": [], "loops": []}') == ["not valid JSON: duplicate key 'loops'"]


def test_an_oversized_document_is_rejected_before_parsing(spec):
    text = contracts_text() + " " * MAX_LOOP_CONTRACTS_CHARS
    assert _problems(spec, text) == [f"{len(text)} characters; at most {MAX_LOOP_CONTRACTS_CHARS}"]


@pytest.mark.parametrize("file", ["include/safe_array.h", "src/other.c", "./src/safe_array.c", "/src/safe_array.c"])
def test_the_file_must_be_an_editable_source(spec, file):
    problems = parse_loop_contracts(spec, contracts_text(loop_entry(file=file))).problems
    assert [problem.kind for problem in problems] == [ContractProblemKind.FILE]
    assert problems[0].describe() == f"loops[0] ({file!r}:{LOOP_LINE}): {file!r} is not in sources.editable"


def test_a_loop_may_have_only_one_contract(spec):
    text = contracts_text(loop_entry(), loop_entry(line=LOOP_LINE + 1), loop_entry())
    problems = parse_loop_contracts(spec, text).problems
    assert [(problem.kind, problem.loop) for problem in problems] == [(ContractProblemKind.DUPLICATE, 2)]
    assert problems[0].describe().endswith("the same loop as loops[0]")


def test_a_rejected_clause_names_its_loop_field_and_reason(spec):
    text = contracts_text(loop_entry(), loop_entry(line=3, invariant=["1", "(hi = lo) >= 0"]))
    contracts = parse_loop_contracts(spec, text)
    assert not contracts.loops
    [problem] = contracts.problems
    assert (problem.kind, problem.loop, problem.clause, problem.rejection) == (
        ContractProblemKind.CLAUSE,
        1,
        "invariant[1]",
        Rejection.ASSIGNMENT,
    )
    assert problem.describe() == (
        f"loops[1] ({SAFE_ARRAY_SOURCE}:3) invariant[1]: assignment: '=': assignment is not allowed: "
        "'(hi = lo) >= 0'"
    )


def test_every_clause_kind_is_checked_with_its_own_grammar(spec):
    entry = loop_entry(
        invariant=["__CPROVER_loop_entry(lo) <= lo"],
        assigns=["lo", "lo + 1"],
        decreases="__CPROVER_loop_entry(hi) - lo",
    )
    problems = parse_loop_contracts(spec, contracts_text(entry)).problems
    assert [(problem.clause, problem.rejection) for problem in problems] == [
        ("assigns[1]", Rejection.NOT_LVALUE),
        ("decreases", Rejection.MISPLACED_FORM),
    ]


def test_macro_names_reach_the_clause_grammar(spec):
    text = contracts_text(loop_entry(invariant=["lo <= LIMIT"]))
    assert parse_loop_contracts(spec, text).accepted
    problems = parse_loop_contracts(spec, text, macro_names=frozenset({"LIMIT"})).problems
    assert [problem.rejection for problem in problems] == [Rejection.MACRO]


@pytest.mark.parametrize("path", OLD_SCHEMA_FIXTURES, ids=lambda path: path.parent.name + "/" + path.name)
def test_the_measured_contract_files_of_the_old_schema_are_rejected(spec, path):
    assert not parse_loop_contracts(spec, path.read_text(encoding="utf-8")).accepted


def test_the_contracts_path_is_recognised_only_with_the_section(spec):
    assert is_loop_contract_path(spec, spec.inductive.contracts)
    assert not is_loop_contract_path(spec, SAFE_ARRAY_SOURCE)
    plain = spec.model_copy(update={"inductive": None})
    assert not is_loop_contract_path(plain, spec.inductive.contracts)


HOSTILE_TEXTS = ("\ud800", "a\nb", "\u202e", "x" * 100_000, "\x00'\"")
MAX_DESCRIBED_CHARS = 2000


def _is_one_bounded_printable_ascii_line(text: str) -> bool:
    return len(text) <= MAX_DESCRIBED_CHARS and all(" " <= character <= "~" for character in text)


@pytest.mark.parametrize("hostile", HOSTILE_TEXTS, ids=range(len(HOSTILE_TEXTS)))
def test_model_text_in_problem_descriptions_is_escaped_and_bounded(spec, hostile):
    documents = [
        contracts_text(loop_entry(file=hostile)),
        contracts_text(loop_entry(file=hostile, invariant=[hostile], assigns=[hostile], decreases=hostile)),
        contracts_text(loop_entry(**{hostile: 1})),
        json.dumps({"loops": [], hostile: 1}),
        json.dumps({hostile: 1}).replace("}", f", {json.dumps(hostile)}: 2}}"),
        contracts_text(loop_entry(invariant=[hostile])),
    ]
    for text in documents:
        problems = parse_loop_contracts(spec, text).problems
        assert problems
        for problem in problems:
            described = problem.describe()
            assert _is_one_bounded_printable_ascii_line(described), described[:200]
            described.encode("utf-8")
