from __future__ import annotations

import posixpath
from collections.abc import Iterable
from typing import Any

from leanc.backends.inductive.harness_calls import body_references
from leanc.backends.inductive.symbols import CODE_TYPE_ID, Symbol, SymbolTable

__all__ = [
    "editable_constant_objects",
    "harness_constant_problems",
    "keeps_initializer",
]

CONSTANT_FLAG = "#constant"
TRUE_ID = "1"
ARRAY_TYPE_IDS = frozenset({"array", "vector"})
TAG_TYPE_IDS = frozenset({"struct_tag", "union_tag"})
RECORD_TYPE_IDS = frozenset({"struct", "union"})
MAX_TYPE_DEPTH = 64
CHOSEN_BY_CODE = (
    "its value is fixed by the code under test (DFCC keeps the initialiser of an object that is const or has const "
    "components), so the harness must not read it"
)


def _named(node: Any) -> dict | None:
    named = node.get("namedSub") if isinstance(node, dict) else None
    return named if isinstance(named, dict) else None


def _id(node: Any) -> str | None:
    value = node.get("id") if isinstance(node, dict) else None
    return value if isinstance(value, str) else None


def _components(record: Any) -> list[Any] | None:
    named = _named(record)
    listed = named.get("components") if named is not None else None
    components = listed.get("sub") if isinstance(listed, dict) else None
    return components if isinstance(components, list) else None


def _component_type(component: Any) -> Any:
    named = _named(component)
    return named.get("type") if named is not None else None


def _record_keeps(record: Any, table: SymbolTable, depth: int) -> bool:
    components = _components(record)
    if components is None:
        return True
    return any(keeps_initializer(_component_type(component), table, depth + 1) for component in components)


def keeps_initializer(node: Any, table: SymbolTable, depth: int = 0) -> bool:
    if depth > MAX_TYPE_DEPTH or not isinstance(node, dict):
        return True
    named = _named(node)
    if named is not None and _id(named.get(CONSTANT_FLAG)) == TRUE_ID:
        return True
    kind = _id(node)
    if kind in ARRAY_TYPE_IDS:
        elements = node.get("sub")
        return not isinstance(elements, list) or not elements or keeps_initializer(elements[0], table, depth + 1)
    if kind in TAG_TYPE_IDS:
        tag = table.get(_id(named.get("identifier")) or "") if named is not None else None
        return tag is None or not tag.is_type or _record_keeps(tag.type_node, table, depth + 1)
    if kind in RECORD_TYPE_IDS:
        return _record_keeps(node, table, depth + 1)
    return kind is None


def _is_editable_object(symbol: Symbol, files: frozenset[str]) -> bool:
    return (
        symbol.is_global_name
        and not symbol.is_type
        and symbol.type_id != CODE_TYPE_ID
        and symbol.defines
        and not symbol.file_local
        and bool(symbol.file)
        and posixpath.normpath(str(symbol.file)) in files
    )


def editable_constant_objects(linked: SymbolTable, editable: Iterable[str]) -> dict[str, Symbol]:
    files = frozenset(posixpath.normpath(rel) for rel in editable)
    return {
        name: symbol
        for name, symbol in linked.items()
        if _is_editable_object(symbol, files) and keeps_initializer(symbol.type_node, linked)
    }


def harness_constant_problems(pp: str, harness_definitions: SymbolTable, objects: dict[str, Symbol]) -> list[str]:
    problems = [
        f"{where}: the harness reads {name}, defined at {symbol.where()} in the editable sources; {CHOSEN_BY_CODE}"
        for name, symbol in objects.items()
        for where in body_references(pp, name)
    ]
    problems += [
        f"{definition.where()}: the initialiser of {definition.name} refers to {name}, defined at "
        f"{objects[name].where()} in the editable sources; {CHOSEN_BY_CODE}"
        for definition in harness_definitions.values()
        for name in sorted(definition.value_symbols & objects.keys())
    ]
    return problems
