from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from leanc.backends.inductive.harness_calls import ENTRY_POINT, ReturnKind

__all__ = [
    "RESERVED_PREFIX",
    "Symbol",
    "SymbolTable",
    "capture_problems",
    "claimed_problems",
    "parse_symbol_table",
    "return_kinds",
    "trusted_definitions",
]

SYMBOL_TABLE_KEY = "symbolTable"
MESSAGE_TYPE_KEY = "messageType"
MESSAGE_TEXT_KEY = "messageText"
ERROR_MESSAGE_TYPE = "ERROR"
LOCAL_SCOPE_SEPARATOR = "::"
PSEUDO_FILE_PREFIX = "<"
NO_VALUE_IDS = frozenset({"", "nil"})
VOID_TYPE_ID = "empty"
CODE_TYPE_ID = "code"
RESERVED_PREFIX = "leanc_"
LINK_RENAME_RE = re.compile(r"^(?P<base>.+)\$link\d+$")


@dataclass(frozen=True)
class Symbol:
    name: str
    base_name: str
    module: str
    file: str | None
    line: str | None
    file_local: bool
    has_value: bool
    static_lifetime: bool
    extern: bool
    is_type: bool
    type_id: str
    return_type_id: str | None

    @property
    def is_global_name(self) -> bool:
        return LOCAL_SCOPE_SEPARATOR not in self.name

    @property
    def defines(self) -> bool:
        return self.has_value or (self.static_lifetime and not self.extern)

    @property
    def real_file(self) -> bool:
        return bool(self.file) and not str(self.file).startswith(PSEUDO_FILE_PREFIX)

    def where(self) -> str:
        return f"{self.file}:{self.line}" if self.file else "no location"


SymbolTable = dict[str, Symbol]


def _named(node: Any, key: str) -> Any:
    named = node.get("namedSub") if isinstance(node, dict) else None
    return named.get(key) if isinstance(named, dict) else None


def _id(node: Any) -> str | None:
    value = node.get("id") if isinstance(node, dict) else None
    return value if isinstance(value, str) else None


def _symbol(name: str, entry: Any) -> Symbol | None:
    if not isinstance(entry, dict):
        return None
    location = entry.get("location")
    symbol_type = entry.get("type")
    return_type = _named(symbol_type, "return_type")
    return Symbol(
        name=name,
        base_name=str(entry.get("baseName", "")),
        module=str(entry.get("module", "")),
        file=_id(_named(location, "file")),
        line=_id(_named(location, "line")),
        file_local=entry.get("isFileLocal") is True,
        has_value=(_id(entry.get("value")) or "") not in NO_VALUE_IDS,
        static_lifetime=entry.get("isStaticLifetime") is True,
        extern=entry.get("isExtern") is True,
        is_type=entry.get("isType") is True,
        type_id=_id(symbol_type) or "",
        return_type_id=_id(return_type) if return_type is not None else None,
    )


def parse_symbol_table(stdout: str) -> SymbolTable | str:
    try:
        messages = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return f"the symbol table is not valid JSON ({exc.msg} at offset {exc.pos})"
    if not isinstance(messages, list):
        return "the symbol table output is not a JSON list"
    errors = [m.get(MESSAGE_TEXT_KEY) for m in messages if isinstance(m, dict) and m.get(MESSAGE_TYPE_KEY) == ERROR_MESSAGE_TYPE]
    if errors:
        return f"goto-instrument reported errors: {errors[:3]}"
    tables = [m[SYMBOL_TABLE_KEY] for m in messages if isinstance(m, dict) and SYMBOL_TABLE_KEY in m]
    if len(tables) != 1 or not isinstance(tables[0], dict):
        return f"expected one {SYMBOL_TABLE_KEY} object, found {len(tables)}"
    table: SymbolTable = {}
    for name, entry in tables[0].items():
        symbol = _symbol(str(name), entry)
        if symbol is None:
            return f"symbol {name!r} is not a JSON object"
        table[symbol.name] = symbol
    return table


def trusted_definitions(table: SymbolTable, module: str) -> SymbolTable:
    return {
        name: symbol
        for name, symbol in table.items()
        if symbol.module == module and symbol.is_global_name and not symbol.is_type and symbol.real_file and symbol.defines
    }


def _binding_problem(trusted: Symbol, linked: Symbol | None) -> str | None:
    if linked is None:
        return f"trusted symbol {trusted.name} ({trusted.where()}) is missing after linking"
    same = (
        linked.file == trusted.file
        and linked.line == trusted.line
        and linked.file_local == trusted.file_local
        and (linked.has_value or not trusted.has_value)
    )
    if same:
        return None
    return (
        f"trusted symbol {trusted.name} defined at {trusted.where()} is bound to a definition at {linked.where()} "
        "after linking"
    )


def _renamed_trusted(linked: SymbolTable, trusted_names: frozenset[str]) -> list[str]:
    problems = []
    for name, symbol in linked.items():
        match = LINK_RENAME_RE.match(name)
        if match and match.group("base") in trusted_names:
            problems.append(
                f"{name} ({symbol.where()}) is a link-time rename of the trusted symbol {match.group('base')}; "
                "a second definition of that name exists"
            )
    return problems


def _normalised(path: str | None) -> str | None:
    return posixpath.normpath(path) if path else None


def _reserved_editable(linked: SymbolTable, editable_files: frozenset[str]) -> list[str]:
    return [
        f"{symbol.name} ({symbol.where()}): editable code must not define names starting with {RESERVED_PREFIX}"
        for symbol in linked.values()
        if _normalised(symbol.file) in editable_files and symbol.base_name.lower().startswith(RESERVED_PREFIX)
    ]


def capture_problems(
    linked: SymbolTable, trusted_units: Iterable[SymbolTable], editable_files: Iterable[str]
) -> list[str]:
    problems: list[str] = []
    trusted_names: set[str] = set()
    for definitions in trusted_units:
        for name, trusted in definitions.items():
            trusted_names.add(name)
            problem = _binding_problem(trusted, linked.get(name))
            if problem is not None:
                problems.append(problem)
    problems += _renamed_trusted(linked, frozenset(trusted_names))
    problems += _reserved_editable(linked, frozenset(posixpath.normpath(path) for path in editable_files))
    return problems


def _claimed_problem(linked: SymbolTable, name: str, editable_files: frozenset[str]) -> str | None:
    symbol = linked.get(name)
    if symbol is None or symbol.type_id != CODE_TYPE_ID or not symbol.has_value:
        return f"claimed function {name} has no definition in the linked program"
    if symbol.file_local:
        return f"claimed function {name} is static ({symbol.where()}); the harness cannot call it"
    if _normalised(symbol.file) not in editable_files:
        return f"claimed function {name} is defined at {symbol.where()}, outside the editable sources"
    return None


def claimed_problems(linked: SymbolTable, claimed: Sequence[str], editable_files: Iterable[str], harness: str) -> list[str]:
    files = frozenset(posixpath.normpath(path) for path in editable_files)
    problems = [problem for name in claimed if (problem := _claimed_problem(linked, name, files)) is not None]
    entry = linked.get(ENTRY_POINT)
    if entry is None or not entry.has_value or _normalised(entry.file) != posixpath.normpath(harness):
        where = entry.where() if entry is not None else "nowhere"
        problems.append(f"{ENTRY_POINT}() must be defined by the harness {harness}, found {where}")
    return problems


def return_kinds(linked: SymbolTable, claimed: Sequence[str]) -> Mapping[str, ReturnKind] | str:
    kinds: dict[str, ReturnKind] = {}
    for name in claimed:
        symbol = linked.get(name)
        if symbol is None or symbol.return_type_id is None:
            return f"the return type of claimed function {name} is not readable from the symbol table"
        kinds[name] = ReturnKind.VOID if symbol.return_type_id == VOID_TYPE_ID else ReturnKind.VALUE
    return kinds
