from __future__ import annotations

import pytest
from loop_contract_support import SECOND_SOURCE, contracts_text, inductive_spec, loop_entry
from spec_inductive_support import INDUCTIVE_CONTRACTS, SAFE_ARRAY_SOURCE, SAFE_ARRAY_SPEC

from leanc.repair.policy import MAX_ANALYZED_CHARS, RULES, check_candidate, check_generated
from leanc.rules.loop_contracts import LOOP_CONTRACT_RULE, LOOP_CONTRACT_RULES

C_SCANNER_RULES = frozenset({"CPROVER_BUILTIN", "SYMBOL_OVERRIDE", "OBFUSCATION", "TOOL_DIVERGENCE", "PRAGMA"})
SOURCE_WITH_MACRO = "#define LIMIT (hi = lo)\nint helper_value(void) { return 1; }\n"
BIG_DEFINE = "#define BIG 1\n"


def _padded_source_with_big_define(size: int) -> str:
    return BIG_DEFINE + "\n" * (size - len(BIG_DEFINE))


@pytest.fixture(scope="module")
def spec():
    return inductive_spec([SAFE_ARRAY_SOURCE, SECOND_SOURCE])


@pytest.fixture(scope="module")
def sources() -> dict[str, str]:
    return {
        SAFE_ARRAY_SOURCE: (SAFE_ARRAY_SPEC.parent / SAFE_ARRAY_SOURCE).read_text(encoding="utf-8"),
        SECOND_SOURCE: SOURCE_WITH_MACRO,
    }


def _contract_violations(decision) -> list[str]:
    return [v.message for v in decision.violations if v.rule == LOOP_CONTRACT_RULE and v.file == INDUCTIVE_CONTRACTS]


def test_the_rule_is_registered_with_the_policy():
    assert RULES[LOOP_CONTRACT_RULE] == LOOP_CONTRACT_RULES[LOOP_CONTRACT_RULE]
    assert "loop-clause grammar" in RULES[LOOP_CONTRACT_RULE]


def test_a_valid_contracts_file_is_accepted_and_not_scanned_as_c(spec, sources):
    text = contracts_text(
        loop_entry(assigns=["lo", "__CPROVER_object_upto(s->acc, 28)", "__CPROVER_object_whole(s)"])
    )
    decision = check_candidate(spec, sources, {**sources, INDUCTIVE_CONTRACTS: text})
    assert decision.allowed, decision.violations
    assert not {v.rule for v in decision.violations} & C_SCANNER_RULES


def test_an_invalid_contracts_file_is_rejected_with_its_reason(spec, sources):
    text = contracts_text(loop_entry(invariant=["0 <= lo && (hi = lo) >= 0"]))
    decision = check_candidate(spec, sources, {**sources, INDUCTIVE_CONTRACTS: text})
    assert not decision.allowed
    [message] = _contract_violations(decision)
    assert "invariant[0]: assignment" in message


def test_a_contract_without_decreases_is_rejected_by_the_policy(spec, sources):
    entry = loop_entry()
    del entry["decreases"]
    decision = check_candidate(spec, sources, {**sources, INDUCTIVE_CONTRACTS: contracts_text(entry)})
    assert _contract_violations(decision) == ["loops.0.decreases: Field required"]


def test_macros_of_the_editable_sources_cannot_be_used_in_clauses(spec, sources):
    text = contracts_text(loop_entry(invariant=["LIMIT >= 0"]))
    decision = check_candidate(spec, sources, {**sources, INDUCTIVE_CONTRACTS: text})
    [message] = _contract_violations(decision)
    assert "macro: LIMIT is a macro" in message


def test_a_macro_added_in_the_same_candidate_counts(spec, sources):
    text = contracts_text(loop_entry(invariant=["lo <= CAP"]))
    new_source = sources[SAFE_ARRAY_SOURCE] + "\n#define CAP 16\n"
    candidate = {**sources, SAFE_ARRAY_SOURCE: new_source, INDUCTIVE_CONTRACTS: text}
    assert any("CAP is a macro" in m for m in _contract_violations(check_candidate(spec, sources, candidate)))
    assert not _contract_violations(check_candidate(spec, sources, {**sources, INDUCTIVE_CONTRACTS: text}))


def test_macros_are_read_only_from_editable_sources_the_policy_analyzes(spec, sources):
    text = contracts_text(loop_entry(invariant=["lo <= BIG"]))
    analyzed = {**sources, SECOND_SOURCE: _padded_source_with_big_define(MAX_ANALYZED_CHARS)}
    oversized = {**sources, SECOND_SOURCE: _padded_source_with_big_define(MAX_ANALYZED_CHARS + 1)}
    assert len(analyzed[SECOND_SOURCE]) == MAX_ANALYZED_CHARS
    assert len(oversized[SECOND_SOURCE]) == MAX_ANALYZED_CHARS + 1
    [message] = _contract_violations(check_candidate(spec, analyzed, {**analyzed, INDUCTIVE_CONTRACTS: text}))
    assert "macro: BIG is a macro" in message
    assert _contract_violations(check_candidate(spec, oversized, {**oversized, INDUCTIVE_CONTRACTS: text})) == []


def test_a_contract_on_a_non_editable_file_is_rejected(spec, sources):
    text = contracts_text(loop_entry(file="include/safe_array.h"))
    decision = check_candidate(spec, sources, {**sources, INDUCTIVE_CONTRACTS: text})
    assert _contract_violations(decision) == [
        "loops[0] ('include/safe_array.h':11): 'include/safe_array.h' is not in sources.editable"
    ]


def test_generation_validates_the_contracts_file_and_keeps_it_out_of_the_target_scan(spec, sources):
    good = check_generated(spec, {**sources, INDUCTIVE_CONTRACTS: contracts_text(loop_entry())})
    assert good.allowed, good.violations
    bad = check_generated(spec, {**sources, INDUCTIVE_CONTRACTS: '{"loops": [{"file": 1}]}'})
    assert not bad.allowed and _contract_violations(bad)
    only_contracts = check_generated(spec, {INDUCTIVE_CONTRACTS: contracts_text()})
    missing = [v for v in only_contracts.violations if v.rule == "MISSING_TARGET"]
    assert missing and INDUCTIVE_CONTRACTS not in missing[0].message


def test_deleting_the_contracts_file_is_still_a_deletion(spec, sources):
    current = {**sources, INDUCTIVE_CONTRACTS: contracts_text()}
    decision = check_candidate(spec, current, sources)
    assert [v.rule for v in decision.violations if v.file == INDUCTIVE_CONTRACTS] == ["FILE_DELETED"]
