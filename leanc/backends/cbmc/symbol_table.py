from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CODE_TYPE_ID",
    "CPROVER_PREFIX",
    "CPROVER_DECLARATION_RULE",
    "SHOW_SYMBOL_TABLE",
    "Symbol",
    "SymbolTable",
    "cprover_declarations",
    "parse_symbol_table",
]

SHOW_SYMBOL_TABLE = ("--show-symbol-table", "--json-ui")
SYMBOL_TABLE_KEY = "symbolTable"
MESSAGE_TYPE_KEY = "messageType"
MESSAGE_TEXT_KEY = "messageText"
ERROR_MESSAGE_TYPE = "ERROR"
LOCAL_SCOPE_SEPARATOR = "::"
PSEUDO_FILE_PREFIX = "<"
NO_VALUE_IDS = frozenset({"", "nil"})
CODE_TYPE_ID = "code"
SYMBOL_EXPRESSION_ID = "symbol"
IDENTIFIER_KEY = "identifier"
SOURCE_LOCATION_KEY = "#source_location"
MAX_LISTED_ERRORS = 3
CPROVER_PREFIX = "__CPROVER_"
CPROVER_DECLARATION_RULE = (
    f"declares a name with the {CPROVER_PREFIX} prefix (function, parameter, local, global or typedef); it would "
    f"shadow or replace the CBMC built-in that LEANC_REACH, LEANC_ASSUME and the inserted checks call, so "
    "probes, assumptions and checks could run harness code instead"
)


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
    type_node: Any = field(default=None, compare=False, hash=False, repr=False)
    value_symbols: frozenset[str] = frozenset()

    @property
    def is_global_name(self) -> bool:
        return LOCAL_SCOPE_SEPARATOR not in self.name

    @property
    def defines(self) -> bool:
        return self.has_value or (self.static_lifetime and not self.extern)

    @property
    def real_file(self) -> bool:
        return bool(self.file) and not str(self.file).startswith(PSEUDO_FILE_PREFIX)

    @property
    def scoped_name(self) -> str:
        return self.name.rsplit(LOCAL_SCOPE_SEPARATOR, 1)[-1]

    def where(self) -> str:
        return f"{self.file}:{self.line}" if self.file else "no location"


SymbolTable = dict[str, Symbol]


def _named(node: Any, key: str) -> Any:
    named = node.get("namedSub") if isinstance(node, dict) else None
    return named.get(key) if isinstance(named, dict) else None


def _id(node: Any) -> str | None:
    value = node.get("id") if isinstance(node, dict) else None
    return value if isinstance(value, str) else None


def _referenced_symbols(value: Any) -> frozenset[str]:
    found: set[str] = set()
    pending = [value]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        named = node.get("namedSub")
        if node.get("id") == SYMBOL_EXPRESSION_ID:
            identifier = _id(named.get(IDENTIFIER_KEY)) if isinstance(named, dict) else None
            if identifier:
                found.add(identifier)
        if isinstance(named, dict):
            pending.extend(child for key, child in named.items() if key != SOURCE_LOCATION_KEY)
        pending.append(node.get("sub"))
    return frozenset(found)


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
        type_node=symbol_type,
        value_symbols=_referenced_symbols(entry.get("value")),
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
        return f"the symbol table run reported errors: {errors[:MAX_LISTED_ERRORS]}"
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


def _declares_cprover_name(symbol: Symbol) -> bool:
    return symbol.real_file and (symbol.base_name.startswith(CPROVER_PREFIX) or symbol.scoped_name.startswith(CPROVER_PREFIX))


def cprover_declarations(table: SymbolTable) -> list[str]:
    return sorted(f"{symbol.where()}: {symbol.name}" for symbol in table.values() if _declares_cprover_name(symbol))
