from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from spec_inductive_support import SAFE_ARRAY_SOURCE, inductive_section, spec_data, spec_with

from leanc.lang.c_expr import ClauseKind
from leanc.specs.model import Spec

INDUCTIVE_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "inductive"
ACCEPTANCE_FIXTURES = INDUCTIVE_FIXTURES / "acceptance"
MUTATION_FIXTURES = ACCEPTANCE_FIXTURES / "mut2"
CLAUSE_FIXTURES = ACCEPTANCE_FIXTURES / "clauses"
CLAUSE_HEADS: dict[ClauseKind, str] = {
    ClauseKind.ASSIGNS: "__CPROVER_assigns(",
    ClauseKind.INVARIANT: "__CPROVER_loop_invariant(",
    ClauseKind.DECREASES: "__CPROVER_decreases(",
}
SECOND_SOURCE = "src/helper.c"
LOOP_LINE = 11


def _closing_parenthesis(text: str, opening: int) -> int:
    depth = 0
    for position in range(opening, len(text)):
        if text[position] == "(":
            depth += 1
        elif text[position] == ")":
            depth -= 1
            if depth == 0:
                return position
    raise AssertionError(f"unbalanced clause at offset {opening}")


def _top_level_parts(text: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for position, character in enumerate(text):
        depth += {"(": 1, "[": 1, ")": -1, "]": -1}.get(character, 0)
        if character == "," and depth == 0:
            parts.append(text[start:position].strip())
            start = position + 1
    return [*parts, text[start:].strip()]


def fixture_clauses(path: Path) -> dict[ClauseKind, list[str]]:
    text = path.read_text(encoding="utf-8")
    found: dict[ClauseKind, list[str]] = {}
    for kind, head in CLAUSE_HEADS.items():
        start = text.find(head)
        if start < 0:
            continue
        opening = start + len(head) - 1
        inner = text[opening + 1 : _closing_parenthesis(text, opening)]
        found[kind] = _top_level_parts(inner) if kind is ClauseKind.ASSIGNS else [inner.strip()]
    return found


def breakout_payload(path: Path) -> str:
    head = CLAUSE_HEADS[ClauseKind.DECREASES]
    line = next(line for line in path.read_text(encoding="utf-8").splitlines() if head in line)
    return line.split(head, 1)[1].rstrip().removesuffix(")")


def inductive_spec(editable: list[str] | None = None) -> Spec:
    data = spec_data()
    data["sources"]["editable"] = editable or [SAFE_ARRAY_SOURCE]
    return spec_with(inductive_section(), data)


def loop_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "file": SAFE_ARRAY_SOURCE,
        "line": LOOP_LINE,
        "invariant": ["0 <= lo && lo <= hi && hi <= len"],
        "assigns": ["lo", "hi"],
        "decreases": "hi - lo",
    }
    entry.update(overrides)
    return entry


def contracts_text(*entries: dict[str, Any]) -> str:
    return json.dumps({"loops": list(entries)})
