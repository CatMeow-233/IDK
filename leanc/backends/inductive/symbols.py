from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Mapping, Sequence

from leanc.backends.cbmc.symbol_table import CODE_TYPE_ID, Symbol, SymbolTable
from leanc.backends.inductive.harness_calls import ENTRY_POINT, ReturnKind

__all__ = [
    "RENAME_REMEDY",
    "RESERVED_PREFIX",
    "capture_problems",
    "claimed_problems",
    "return_kinds",
    "trusted_definitions",
]

VOID_TYPE_ID = "empty"
RESERVED_PREFIX = "leanc_"
LINK_RENAME_RE = re.compile(r"^(?P<base>.+)\$link\d+$")
RENAME_REMEDY = (
    "a name the harness translation unit defines, its headers included, must not be defined again by the editable "
    "sources, not even as a static: rename the editable one"
)


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
                f"a second definition of that name exists ({RENAME_REMEDY})"
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
