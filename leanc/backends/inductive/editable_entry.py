from __future__ import annotations

import posixpath
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from leanc.backends.cbmc.symbol_table import CODE_TYPE_ID, SymbolTable
from leanc.backends.inductive.harness_calls import CLAIMED_CALLS_COUNTER, CLAIMED_ENTERED_FLAG, EDITABLE_ENTERED_FLAG
from leanc.lang.c_lexer import CSource
from leanc.rules.definitions import function_definition_sites
from leanc.rules.loop_sites import BODY_OPEN, Insertion, origin_lines

__all__ = [
    "EDITABLE_BEFORE_CLAIMED_PREFIX",
    "EntryFlags",
    "editable_before_claimed_label",
    "editable_includes",
    "entry_flags",
    "uninstrumented_functions",
]

EDITABLE_BEFORE_CLAIMED_PREFIX = "leanc_editable_before_claimed_call:"
FIRST_CLAIMED_CALL = 1


@dataclass(frozen=True)
class EntryFlags:
    insertions: tuple[Insertion, ...] = ()
    functions: frozenset[tuple[str, str]] = frozenset()
    problems: tuple[str, ...] = ()


def editable_before_claimed_label(function: str) -> str:
    return f"{EDITABLE_BEFORE_CLAIMED_PREFIX}{function}"


def _claimed_entry_text(function: str) -> str:
    check = f'__CPROVER_assert({EDITABLE_ENTERED_FLAG} == 0, "{editable_before_claimed_label(function)}")'
    return (
        f" extern int {CLAIMED_CALLS_COUNTER}, {CLAIMED_ENTERED_FLAG}, {EDITABLE_ENTERED_FLAG}; "
        f"if ({CLAIMED_CALLS_COUNTER} == {FIRST_CLAIMED_CALL} && !{CLAIMED_ENTERED_FLAG}) "
        f"{{ {check}; {CLAIMED_ENTERED_FLAG} = 1; }} "
    )


def _unclaimed_entry_text() -> str:
    return (
        f" extern int {CLAIMED_ENTERED_FLAG}, {EDITABLE_ENTERED_FLAG}; "
        f"if (!{CLAIMED_ENTERED_FLAG}) {EDITABLE_ENTERED_FLAG} = 1; "
    )


def _entry_text(name: str, claimed: frozenset[str]) -> str:
    return _claimed_entry_text(name) if name in claimed else _unclaimed_entry_text()


def entry_flags(rel: str, pp: str, claimed: Iterable[str], editable: Sequence[str]) -> EntryFlags:
    source = CSource(pp)
    if source.text != pp:
        return EntryFlags(problems=(f"{rel}: the preprocessed unit still has a line splice",))
    files = frozenset(posixpath.normpath(path) for path in editable)
    origins = origin_lines(pp)
    claimed_names = frozenset(claimed)
    insertions: list[Insertion] = []
    names: set[tuple[str, str]] = set()
    problems: list[str] = []
    for site in function_definition_sites(source):
        origin = origins[source.line_of(site.offset) - 1]
        if origin is None or posixpath.normpath(origin.file) not in files:
            continue
        if pp[site.body_open] != BODY_OPEN:
            problems.append(f"{origin.file}:{origin.line}: the body of {site.name} does not open with {BODY_OPEN}")
            continue
        insertions.append(Insertion(site.body_open + 1, _entry_text(site.name, claimed_names)))
        names.add((posixpath.normpath(origin.file), site.name))
    return EntryFlags(tuple(insertions), frozenset(names), tuple(problems))


def _normalised(path: str | None) -> str | None:
    return posixpath.normpath(path) if path else None


def uninstrumented_functions(
    linked: SymbolTable, instrumented: frozenset[tuple[str, str]], editable: Sequence[str]
) -> list[str]:
    files = frozenset(posixpath.normpath(rel) for rel in editable)
    return [
        f"{symbol.base_name or symbol.name}() ({symbol.where()}) is a function of the editable sources without the "
        "inserted entry check"
        for symbol in linked.values()
        if symbol.type_id == CODE_TYPE_ID
        and symbol.has_value
        and not symbol.is_type
        and _normalised(symbol.file) in files
        and (_normalised(symbol.file), symbol.base_name or symbol.name) not in instrumented
    ]


def editable_includes(pp: str, editable: Sequence[str]) -> list[str]:
    files = frozenset(posixpath.normpath(rel) for rel in editable)
    seen = dict.fromkeys(
        origin.file for origin in origin_lines(pp) if origin is not None and posixpath.normpath(origin.file) in files
    )
    return [f"the harness translation unit includes the editable source {rel}" for rel in seen]
