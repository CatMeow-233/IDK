from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from leanc.rules.loop_contracts import LoopContract
from leanc.rules.loop_sites import (
    FunctionEntry,
    Insertion,
    MatchedLoop,
    annotate,
    contract_insertion,
    function_entries,
    function_probe_insertion,
    function_probe_label,
    loop_probe_insertion,
    loop_probe_label,
    loop_sites,
)


@dataclass(frozen=True)
class EditableUnit:
    index: int
    rel: str
    pp: str
    source: str


@dataclass(frozen=True)
class ContractMatch:
    matched: tuple[MatchedLoop, ...] = ()
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClaimedEntries:
    entries: tuple[FunctionEntry, ...] = ()
    problems: tuple[str, ...] = ()


def match_contracts(units: Sequence[EditableUnit], contracts: Sequence[LoopContract]) -> ContractMatch:
    matched: list[MatchedLoop] = []
    problems: list[str] = []
    for unit in units:
        found = loop_sites(unit.rel, unit.pp, unit.source, contracts)
        matched += found.matched
        problems += [problem.describe() for problem in found.problems]
    located = {(item.contract.file, item.contract.line) for item in matched}
    for contract in contracts:
        reported = any(problem.startswith(f"{contract.location}:") for problem in problems)
        if (contract.file, contract.line) not in located and not reported:
            problems.append(f"{contract.location}: no editable translation unit holds this loop")
    return ContractMatch(tuple(matched), tuple(problems))


def claimed_entries(units: Sequence[EditableUnit], claimed: Sequence[str]) -> ClaimedEntries:
    entries: list[FunctionEntry] = []
    problems: list[str] = []
    for unit in units:
        found = function_entries(unit.rel, unit.pp, claimed)
        entries += found.entries
        problems += [problem.describe() for problem in found.problems]
    for name in claimed:
        definitions = [entry for entry in entries if entry.name == name]
        if len(definitions) != 1:
            where = ", ".join(f"{entry.file}:{entry.line}" for entry in definitions) or "none"
            problems.append(
                f"claimed function {name} must have exactly one definition in the editable sources, found "
                f"{len(definitions)} ({where})"
            )
    return ClaimedEntries(tuple(entries), tuple(problems))


def _in_unit(unit: EditableUnit, matched: Sequence[MatchedLoop]) -> list[MatchedLoop]:
    return [item for item in matched if item.site.file == unit.rel]


def verify_text(unit: EditableUnit, matched: Sequence[MatchedLoop]) -> str:
    return annotate(unit.pp, [contract_insertion(item) for item in _in_unit(unit, matched)])


def vacuity_text(unit: EditableUnit, matched: Sequence[MatchedLoop], entries: Sequence[FunctionEntry]) -> str:
    loops = _in_unit(unit, matched)
    insertions: list[Insertion] = [contract_insertion(item) for item in loops]
    insertions += [loop_probe_insertion(item) for item in loops]
    insertions += [function_probe_insertion(entry) for entry in entries if entry.file == unit.rel]
    return annotate(unit.pp, insertions)


def loop_probe_labels(matched: Sequence[MatchedLoop]) -> list[str]:
    return [loop_probe_label(item.site.file, item.site.line) for item in matched]


def function_probe_labels(claimed: Sequence[str]) -> list[str]:
    return [function_probe_label(name) for name in claimed]
