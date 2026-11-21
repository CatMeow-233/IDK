from __future__ import annotations

from pathlib import Path

import pytest

from leanc.repair.policy import RULES as POLICY_RULES
from leanc.repair.policy import check_candidate, check_generated
from leanc.rules.acsl import (
    ACSL_RULES,
    find_annotations,
    parse_clauses,
    target_contracts,
)
from leanc.rules.c_rules import RULES as INTEGRITY_RULES
from leanc.rules.source import source_violations
from leanc.specs.loader import load_spec

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
EX = "wp_safe_array_get"
REL = "src/safe_array.c"
GOOD = (EXAMPLES / EX / REL).read_text()
HEADERS = {"include/safe_array.h": (EXAMPLES / EX / "include/safe_array.h").read_text()}
TARGET_DEF = "int safe_array_get(const int *arr"


@pytest.fixture(scope="module")
def spec():
    return load_spec(EXAMPLES / EX / "spec.yaml")


def rules(spec, text, headers=HEADERS):
    return {v.rule for v in source_violations(spec, REL, text, headers=headers)}


def body(stmt: str) -> str:
    return GOOD.replace("    *out = arr[idx];", stmt + "\n    *out = arr[idx];")


def helper(contract: str, decl: str = "static int id(int n)") -> str:
    return GOOD.replace(TARGET_DEF, f"{contract}\n{decl} {{ return n; }}\n\n{TARGET_DEF}")


ALLOWED = {
    "assert": body("    /*@ assert idx < len; */"),
    "check": body("    //@ check \\forall integer k; 0 <= k < 1 ==> k == 0;"),
    "loop annotations": GOOD.replace(
        "    *out = arr[idx];",
        "    size_t i = 0;\n"
        "    /*@ loop invariant 0 <= i <= idx;\n"
        "      @ check loop invariant i <= idx;\n"
        "      @ loop assigns i;\n"
        "      @ loop variant idx - i; */\n"
        "    while (i < idx) i++;\n"
        "    *out = arr[idx];",
    ),
    "for-behaviour assert": body("    /*@ for in_range: assert idx < len; */"),
    "static helper contract": helper("/*@ requires n >= 0;\n  @ assigns \\nothing;\n  @ ensures \\result == n; */"),
    "helper contract as //@ lines": helper("//@ requires n >= 0;\n//@ ensures \\result == n;"),
    "helper behaviours": helper(
        "/*@ behavior pos: assumes n > 0; ensures \\result > 0;\n    behavior neg: assumes n <= 0; "
        "ensures \\result <= 0;\n    complete behaviors; disjoint behaviors; */"
    ),
    "logic definitions and lemmas": "/*@ predicate in_range(integer i, integer n) = 0 <= i < n;\n"
    "    logic integer twice(integer x) = 2 * x;\n"
    "    lemma tw: \\forall integer x; twice(x) == x + x;\n"
    "    check lemma tw2: twice(1) == 2; */\n" + GOOD,
    "ACSL-looking string": GOOD + 'static const char *s = "/*@ admit \\\\false; */";\n',
    "plain comment with @": GOOD + "/* @ admit \\false; */\n/** @brief doc */\n",
}


@pytest.mark.parametrize("name", sorted(ALLOWED))
def test_proof_obligations_are_allowed(spec, name):
    text = ALLOWED[name]
    assert source_violations(spec, REL, text, headers=HEADERS) == []
    assert check_generated(spec, {REL: text}).allowed
    if text != GOOD:
        d = check_candidate(spec, {REL: GOOD}, {REL: text})
        assert d.allowed, d.violations


REJECTED = {
    "requires false on target": (GOOD.replace(TARGET_DEF, "/*@ requires \\false; */\n" + TARGET_DEF), "ACSL_CONTRACT"),
    "ensures on target": (GOOD.replace(TARGET_DEF, "/*@ ensures \\result == 0; */\n" + TARGET_DEF), "ACSL_CONTRACT"),
    "assigns on target": (GOOD.replace(TARGET_DEF, "//@ assigns \\nothing;\n" + TARGET_DEF), "ACSL_CONTRACT"),
    "contract before #include": ("/*@ requires \\false; */\n" + GOOD, "ACSL_CONTRACT"),
    "contract on a prototype": (
        GOOD.replace(TARGET_DEF, "/*@ ensures \\result == 0; */\nsize_t strlen(const char *s);\n" + TARGET_DEF),
        "ACSL_CONTRACT",
    ),
    "non-static helper": (helper("/*@ ensures \\result == n; */", "int id(int n)"), "ACSL_CONTRACT"),
    "requires false on helper": (helper("/*@ requires n > 0 && \\false; */"), "ACSL_CONTRACT"),
    "admit assertion": (body("    /*@ admit idx < len; */"), "ACSL_FORBIDDEN"),
    "admit loop invariant": (body("    /*@ admit loop invariant \\true; */"), "ACSL_FORBIDDEN"),
    "admit ensures on helper": (helper("/*@ admit ensures \\result == 0; */"), "ACSL_FORBIDDEN"),
    "admit lemma": ("/*@ admit lemma cheat: \\false; */\n" + GOOD, "ACSL_FORBIDDEN"),
    "@-prefixed admit": (body("    /*@ @admit idx < len; */"), "ACSL_FORBIDDEN"),
    "axiomatic": (GOOD + "/*@ axiomatic A { axiom bad: \\false; } */\n", "ACSL_FORBIDDEN"),
    "inductive": ("/*@ inductive P(integer x) { case c: \\forall integer x; P(x); } */\n" + GOOD, "ACSL_FORBIDDEN"),
    "recursive logic": ("/*@ logic integer f(integer x) = f(x) + 1; */\n" + GOOD, "ACSL_FORBIDDEN"),
    "abstract predicate": ("//@ predicate P(integer x);\n" + GOOD, "ACSL_FORBIDDEN"),
    "ghost code": (body("    //@ ghost int g = 0;"), "ACSL_FORBIDDEN"),
    "statement contract": (body("    /*@ requires idx < len; ensures \\true; */"), "ACSL_FORBIDDEN"),
    "global invariant": ("/*@ global invariant gi: 1 == 1; */\n" + GOOD, "ACSL_FORBIDDEN"),
    "type definition": ("/*@ type t = integer; */\n" + GOOD, "ACSL_FORBIDDEN"),
    "macro inside annotation": (body("#define HACK \\true\n    /*@ assert HACK; */"), "ACSL_MACRO"),
    "redefined ACSL type": ("#define integer char\n" + GOOD, "ACSL_MACRO"),
    "redefined contract identifier": ("#define len 3\n" + GOOD, "ACSL_MACRO"),
}


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_assumption_injecting_acsl_is_rejected(spec, name):
    text, rule = REJECTED[name]
    assert rule in rules(spec, text), source_violations(spec, REL, text, headers=HEADERS)
    if rule != "ACSL_MACRO" or "HACK" in text:
        d = check_candidate(spec, {REL: GOOD}, {REL: text})
        assert not d.allowed and rule in {v.rule for v in d.violations}
    vs = [v for v in source_violations(spec, REL, text, headers=HEADERS) if v.rule == rule]
    assert all(v.file == REL and v.line and v.message for v in vs)


def test_rules_are_catalogued():
    for r in ACSL_RULES:
        assert r in INTEGRITY_RULES and r in POLICY_RULES


def test_rules_apply_only_with_the_wp_gate():
    spec = load_spec(EXAMPLES / "safe_array_get" / "spec.yaml")
    text = GOOD.replace(TARGET_DEF, "//@{ doxygen group\n/*@ requires \\false; */\n" + TARGET_DEF) + "//@}\n"
    assert not {v.rule for v in source_violations(spec, REL, text)} & set(ACSL_RULES)


def test_lexing_and_clauses():
    text = 'int x; /* /*@ not */ "//@ no" //@ assert x;\n/*@ loop invariant \\forall integer k; 0 <= k < n ==> a[k];\n @ loop assigns i; */'
    anns = find_annotations(text)
    assert [a.line_comment for a in anns] == [True, False]
    clauses = parse_clauses(anns[1].body)
    assert [c.tokens[:2] for c in clauses] == [["loop", "invariant"], ["loop", "assigns"]]


def test_target_contract_is_found_in_protected_header():
    (c,) = target_contracts("safe_array_get", HEADERS)
    assert c.file == "include/safe_array.h" and c.has_obligations
    assert c.clauses["ensures"] == 6 and c.behaviors == ["null_input", "out_of_range", "in_range"]
    plain = {"include/safe_array.h": (EXAMPLES / "safe_array_get/include/safe_array.h").read_text()}
    assert target_contracts("safe_array_get", plain) == []


def test_repair_prompt_explains_the_acsl_rules_for_wp_specs(spec):
    from leanc.llm.base import RepairRequest
    from leanc.llm.prompts import build_repair_prompt
    from leanc.specs.loader import spec_to_yaml

    def prompt(s):
        req = RepairRequest(requirement=s.requirement, spec=s, spec_yaml=spec_to_yaml(s), round=1,
                            sources={REL: GOOD}, headers=HEADERS, harness=None, tests=None, diagnostics=[],
                            failures=[], history=[])
        return "\n".join(m["content"] for m in build_repair_prompt(req).messages())

    text = prompt(spec)
    assert "Frama-C/WP proof obligations" in text and "loop invariant" in text and "admit" in text
    assert "Termination is required" in text
    assert "Frama-C/WP proof obligations" not in prompt(load_spec(EXAMPLES / "safe_array_get" / "spec.yaml"))
