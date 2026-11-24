from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from leanc.backends.counterexample.locations import location_from_cbmc
from leanc.backends.counterexample.properties import property_class
from leanc.backends.inductive.loops import BUILTIN_LIBRARY_PREFIX
from leanc.backends.inductive.problems import BuildProblem, contract_rejected, tool_error
from leanc.rules.loop_sites import MatchedLoop

LOOP_PROPERTY_CLASSES: tuple[str, ...] = (
    "loop_invariant_base",
    "loop_invariant_step",
    "loop_decreases",
    "loop_assigns",
    "loop_step_unwinding",
)
_LOOP_NAME_RE = re.compile(r"\bfor loop (?P<loop>\S+)$")


@dataclass
class EvidencedLoop:
    name: str
    file: str | None
    function: str | None
    lines: set[int] = field(default_factory=set)
    classes: set[str] = field(default_factory=set)

    def where(self) -> str:
        line = min(self.lines) if self.lines else "?"
        return f"{self.file or '?'}:{line} (CBMC loop {self.name})"


@dataclass(frozen=True)
class LoopEvidence:
    loops: dict[str, EvidencedLoop] = field(default_factory=dict)
    problem: BuildProblem | None = None

    def loop_of(self, contract_location: str) -> EvidencedLoop | None:
        return self.loops.get(contract_location)


def _normalised(path: str | None) -> str | None:
    return posixpath.normpath(path) if path else None


def is_loop_property(result: dict) -> bool:
    return property_class(result.get("property"), result.get("description")) in LOOP_PROPERTY_CLASSES


def _loop_name(result: dict) -> str | None:
    match = _LOOP_NAME_RE.search(str(result.get("description") or ""))
    return match.group("loop") if match else None


def _collect(results: Sequence[dict]) -> tuple[dict[str, EvidencedLoop], list[str]]:
    loops: dict[str, EvidencedLoop] = {}
    unnamed: list[str] = []
    for result in results:
        if not is_loop_property(result):
            continue
        name = _loop_name(result)
        location = location_from_cbmc(result.get("sourceLocation"))
        if name is None or location is None or location.line is None:
            unnamed.append(str(result.get("property")))
            continue
        loop = loops.setdefault(name, EvidencedLoop(name, _normalised(location.file), location.function))
        if loop.file != _normalised(location.file):
            unnamed.append(f"{result.get('property')} (loop {name} reported in two files)")
        loop.lines.add(location.line)
        loop.classes.add(property_class(result.get("property"), result.get("description")))
    return loops, unnamed


def _trusted_file(file: str | None, harness: str) -> bool:
    return bool(file) and (str(file).startswith(BUILTIN_LIBRARY_PREFIX) or file == posixpath.normpath(harness))


def _covers(item: MatchedLoop, loop: EvidencedLoop) -> bool:
    site = item.site
    return loop.file == posixpath.normpath(site.file) and all(site.line <= line <= site.header_end_line for line in loop.lines)


@dataclass
class _Matcher:
    contracts: Sequence[MatchedLoop]
    harness: str
    errors: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    by_contract: dict[str, list[EvidencedLoop]] = field(default_factory=dict)

    def assign(self, loop: EvidencedLoop) -> None:
        if _trusted_file(loop.file, self.harness):
            return
        owners = [item for item in self.contracts if _covers(item, loop)]
        if len(owners) != 1:
            self.errors.append(
                f"{loop.where()} carries loop-contract properties but matches {len(owners)} contract(s) of the "
                "contracts file; every contracted loop must come from exactly one contract"
            )
            return
        self.by_contract.setdefault(owners[0].contract.location, []).append(loop)

    def judge(self, item: MatchedLoop) -> EvidencedLoop | None:
        location = item.contract.location
        found = self.by_contract.get(location, [])
        if not found:
            self.rejected.append(
                f"{location}: CBMC reports no loop-contract property for this loop, so it is not reached from "
                "main() of the inductive harness; remove its contract"
            )
            return None
        if len(found) > 1:
            self.errors.append(f"{location}: the contract is applied to {len(found)} CBMC loops: {', '.join(loop.where() for loop in found)}")
            return None
        missing = [name for name in LOOP_PROPERTY_CLASSES if name not in found[0].classes]
        if missing:
            self.errors.append(f"{location}: CBMC reports no {', '.join(missing)} property for {found[0].where()}")
            return None
        return found[0]

    def problem(self) -> BuildProblem | None:
        if self.errors:
            return tool_error("the loop-contract properties of CBMC do not match the contracts one to one", self.errors)
        if self.rejected:
            return contract_rejected("a loop contract is not exercised by the inductive harness", self.rejected)
        return None


def loop_evidence(results: Sequence[dict], contracts: Sequence[MatchedLoop], harness: str) -> LoopEvidence:
    loops, unnamed = _collect(results)
    matcher = _Matcher(contracts, harness)
    if unnamed:
        matcher.errors.append("loop-contract properties without a CBMC loop name or location: " + ", ".join(unnamed))
    for loop in loops.values():
        matcher.assign(loop)
    evidenced = {}
    for item in contracts:
        loop = matcher.judge(item)
        if loop is not None:
            evidenced[item.contract.location] = loop
    return LoopEvidence(evidenced, matcher.problem())
