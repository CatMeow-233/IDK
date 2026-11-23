from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from leanc.foundation.errors import JSON_LOAD_ERRORS
from leanc.foundation.models import PolicyViolation
from leanc.lang.c_expr import AssignsTarget, Clause, ClauseKind, Expression, parse_clause
from leanc.lang.c_expr_lexer import ClauseRejected, Rejection
from leanc.specs.loader import json_object_rejecting_duplicate_keys
from leanc.specs.model import Spec

__all__ = [
    "LOOP_CONTRACT_RULE",
    "LOOP_CONTRACT_RULES",
    "MAX_CLAUSES_PER_LIST",
    "MAX_CONTRACTED_LOOPS",
    "MAX_LOOP_CONTRACTS_CHARS",
    "MAX_LOOP_LINE",
    "ContractProblem",
    "ContractProblemKind",
    "LoopContract",
    "LoopContracts",
    "is_loop_contract_path",
    "loop_contract_violations",
    "parse_loop_contracts",
]

LOOP_CONTRACT_RULE = "LOOP_CONTRACT"
LOOP_CONTRACT_RULES: dict[str, str] = {
    LOOP_CONTRACT_RULE: "a loop-contract file (inductive.contracts) that is not valid JSON, breaks the schema "
    '{"loops": [{"file", "line", "invariant", "assigns", "decreases"}]}, names a file outside sources.editable, '
    "repeats a (file, line) or has a clause outside the loop-clause grammar",
}

MAX_LOOP_CONTRACTS_CHARS = 1024 * 1024
MAX_CONTRACTED_LOOPS = 64
MAX_CLAUSES_PER_LIST = 16
MAX_LOOP_LINE = 10_000_000
MAX_SHOWN_TEXT_CHARS = 120
MAX_SHOWN_MESSAGE_CHARS = 200
MAX_SHOWN_SCHEMA_ERRORS = 20
_DOCUMENT_LOCATION = "<contracts>"
_PLAIN_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LoopContractEntry(_StrictModel):
    file: str
    line: int = Field(ge=1, le=MAX_LOOP_LINE)
    invariant: list[str] = Field(min_length=1, max_length=MAX_CLAUSES_PER_LIST)
    assigns: list[str] = Field(min_length=1, max_length=MAX_CLAUSES_PER_LIST)
    decreases: str


class LoopContractsDocument(_StrictModel):
    loops: list[LoopContractEntry] = Field(max_length=MAX_CONTRACTED_LOOPS)


class ContractProblemKind(str, Enum):
    DOCUMENT = "document"
    FILE = "file"
    DUPLICATE = "duplicate"
    CLAUSE = "clause"


@dataclass(frozen=True)
class ContractProblem:
    kind: ContractProblemKind
    message: str
    loop: int | None = None
    location: str = ""
    clause: str = ""
    rejection: Rejection | None = None

    def describe(self) -> str:
        where = [] if self.loop is None else [f"loops[{self.loop}]"]
        where += [part for part in (f"({self.location})" if self.location else "", self.clause) if part]
        return f"{' '.join(where)}: {self.message}" if where else self.message


@dataclass(frozen=True)
class LoopContract:
    file: str
    line: int
    invariant: tuple[Expression, ...]
    assigns: tuple[AssignsTarget, ...]
    decreases: Expression

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}"

    def annotation(self) -> str:
        assigns = ", ".join(target.render() for target in self.assigns)
        invariant = " && ".join(f"({entry.render()})" for entry in self.invariant)
        return (
            f" __CPROVER_assigns({assigns}) __CPROVER_loop_invariant({invariant})"
            f" __CPROVER_decreases({self.decreases.render()}) "
        )


@dataclass(frozen=True)
class LoopContracts:
    loops: tuple[LoopContract, ...] = ()
    problems: tuple[ContractProblem, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.problems


def _shown(text: str) -> str:
    return ascii(text[:MAX_SHOWN_TEXT_CHARS])


def _escaped_message(text: str) -> str:
    return ascii(text[:MAX_SHOWN_MESSAGE_CHARS])[1:-1]


def _shown_location_part(part: object) -> str:
    text = str(part)
    return text if isinstance(part, int) or _PLAIN_KEY_RE.fullmatch(text) else _shown(text)


def _document_problem(message: str) -> ContractProblem:
    return ContractProblem(ContractProblemKind.DOCUMENT, message)


def _schema_problems(exc: ValidationError) -> list[ContractProblem]:
    problems = []
    for error in exc.errors()[:MAX_SHOWN_SCHEMA_ERRORS]:
        location = ".".join(_shown_location_part(part) for part in error.get("loc", ())) or _DOCUMENT_LOCATION
        problems.append(_document_problem(f"{location}: {_escaped_message(str(error.get('msg')))}"))
    return problems


def _decoded_document(text: str) -> LoopContractsDocument | list[ContractProblem]:
    if len(text) > MAX_LOOP_CONTRACTS_CHARS:
        return [_document_problem(f"{len(text)} characters; at most {MAX_LOOP_CONTRACTS_CHARS}")]
    try:
        data: Any = json.loads(text, object_pairs_hook=json_object_rejecting_duplicate_keys)
    except JSON_LOAD_ERRORS as exc:
        return [_document_problem(f"not valid JSON: {_escaped_message(str(exc))}")]
    try:
        return LoopContractsDocument.model_validate(data)
    except ValidationError as exc:
        return _schema_problems(exc)


class _EntryChecker:
    def __init__(self, spec: Spec, macro_names: frozenset[str]) -> None:
        self._editable = frozenset(spec.sources.editable)
        self._macro_names = macro_names
        self._seen: dict[tuple[str, int], int] = {}
        self.problems: list[ContractProblem] = []

    def checked(self, position: int, entry: LoopContractEntry) -> LoopContract | None:
        shown_file = entry.file if entry.file in self._editable else _shown(entry.file)
        location = f"{shown_file}:{entry.line}"
        problems_before = len(self.problems)
        if entry.file not in self._editable:
            message = f"{_shown(entry.file)} is not in sources.editable"
            self._add(ContractProblemKind.FILE, message, position, location)
        first = self._seen.setdefault((entry.file, entry.line), position)
        if first != position:
            self._add(ContractProblemKind.DUPLICATE, f"the same loop as loops[{first}]", position, location)
        invariant = self._parsed_list(position, location, ClauseKind.INVARIANT, entry.invariant)
        assigns = self._parsed_list(position, location, ClauseKind.ASSIGNS, entry.assigns)
        decreases_kind = ClauseKind.DECREASES
        decreases = self._parsed_clause(position, location, decreases_kind.value, decreases_kind, entry.decreases)
        if len(self.problems) > problems_before:
            return None
        return LoopContract(entry.file, entry.line, tuple(invariant), tuple(assigns), decreases)

    def _parsed_list(self, position: int, location: str, kind: ClauseKind, texts: list[str]) -> list[Clause | None]:
        return [
            self._parsed_clause(position, location, f"{kind.value}[{index}]", kind, text)
            for index, text in enumerate(texts)
        ]

    def _parsed_clause(self, position: int, location: str, label: str, kind: ClauseKind, text: str) -> Clause | None:
        try:
            return parse_clause(text, kind, macro_names=self._macro_names)
        except ClauseRejected as exc:
            self.problems.append(
                ContractProblem(
                    ContractProblemKind.CLAUSE,
                    f"{exc}: {_shown(text)}",
                    loop=position,
                    location=location,
                    clause=label,
                    rejection=exc.rejection,
                )
            )
            return None

    def _add(self, kind: ContractProblemKind, message: str, position: int, location: str) -> None:
        self.problems.append(ContractProblem(kind, message, loop=position, location=location))


def parse_loop_contracts(
    spec: Spec, text: str | None, *, macro_names: frozenset[str] = frozenset()
) -> LoopContracts:
    if text is None:
        return LoopContracts()
    document = _decoded_document(text)
    if isinstance(document, list):
        return LoopContracts(problems=tuple(document))
    checker = _EntryChecker(spec, macro_names)
    loops = [checker.checked(position, entry) for position, entry in enumerate(document.loops)]
    if checker.problems:
        return LoopContracts(problems=tuple(checker.problems))
    return LoopContracts(loops=tuple(loop for loop in loops if loop is not None))


def is_loop_contract_path(spec: Spec, path: str) -> bool:
    return spec.inductive is not None and path == spec.inductive.contracts


def loop_contract_violations(
    spec: Spec, path: str, text: str, *, macro_names: frozenset[str] = frozenset()
) -> list[PolicyViolation]:
    contracts = parse_loop_contracts(spec, text, macro_names=macro_names)
    return [
        PolicyViolation(rule=LOOP_CONTRACT_RULE, message=problem.describe(), file=path)
        for problem in contracts.problems
    ]
