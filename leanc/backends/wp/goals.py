"""Goals of the WP JSON report: classification, per-verdict tally and the check that the approved contract applied."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import NamedTuple

from leanc.rules.acsl import TargetContract

SMOKE_PREFIX = "wp_smoke_"
RTE_PREFIX = "assert_rte_"
FRAMAC_SHARE_PREFIX = "FRAMAC_SHARE/"
VALID = "valid"
INVALID = "invalid"
PROVER_FAILED = "failed"
NO_VERDICT = "none"
CLAUSE_KINDS: tuple[tuple[str, str], ...] = (
    ("ensures", "post-condition"),
    ("assigns", "assigns clause (frame condition)"),
    ("exits", "exits clause"),
    ("terminates", "termination"),
    ("decreases", "decreases clause"),
    ("complete_", "completeness of behaviors"),
    ("disjoint_", "disjointness of behaviors"),
    ("assert", "assertion"),
    ("check", "check"),
)
LEMMA_GOAL_PREFIXES = ("typed_lemma", "lemma")

_CALL_SITE_PRECONDITION_RE = re.compile(r"call_(\w+?)_(?:\w+_)?requires")
_RTE_ORDINAL_RE = re.compile(r"_\d+$")
_ENSURES_PROPERTY_RE = re.compile(r"(^|_)ensures(_|$)")
_ASSIGNS_PROPERTY_RE = re.compile(r"(^|_)assigns(_|$)")


def _property_after_function(goal_name: str, prop: str, function: str) -> str:
    if function and f"_{function}_" in goal_name:
        return goal_name.split(f"_{function}_", 1)[1]
    if function and prop.startswith(function + "_"):
        return prop[len(function) + 1 :]
    return prop


def goal_property(goal: dict) -> str:
    """Property name without function and behaviour prefixes, e.g. ``ensures_2`` or ``assert_rte_mem_access``."""
    name = str(goal.get("goal") or "")
    prop = _property_after_function(name, str(goal.get("property") or name), str(goal.get("function") or ""))
    if SMOKE_PREFIX in prop:
        prop = prop[prop.index(SMOKE_PREFIX) :]
    behavior = goal.get("behavior")
    if behavior and prop.startswith(f"{behavior}_"):
        prop = prop[len(behavior) + 1 :]
    return prop


def _loop_invariant_phase(prop: str) -> str:
    if prop.endswith("established"):
        return " (established)"
    if prop.endswith("preserved"):
        return " (preserved)"
    return ""


def goal_kind(goal: dict) -> str:
    """Human-readable class of a WP goal."""
    prop = goal_property(goal)
    if prop.startswith(SMOKE_PREFIX):
        return "smoke test"
    if prop.startswith(RTE_PREFIX):
        guard = _RTE_ORDINAL_RE.sub("", prop[len(RTE_PREFIX) :]).replace("_", " ")
        return f"runtime-error guard ({guard})"
    if prop.startswith("loop_invariant") or prop.startswith("check_loop_invariant"):
        return "loop invariant" + _loop_invariant_phase(prop)
    if prop.startswith("loop_assigns"):
        return "loop assigns clause"
    if prop.startswith("loop_variant"):
        return "loop variant"
    call_site = _CALL_SITE_PRECONDITION_RE.match(prop)
    if call_site:
        return f"precondition of {call_site.group(1)}() at a call site"
    for prefix, kind in CLAUSE_KINDS:
        if prop.startswith(prefix):
            return kind
    if str(goal.get("goal", "")).startswith(LEMMA_GOAL_PREFIXES) or prop.startswith("lemma"):
        return "lemma"
    if "requires" in prop:
        return "precondition"
    return "proof obligation"


def _snapshot_relative(path: str | None, snapshot: str, share: str | None) -> str | None:
    if not path:
        return None
    real = os.path.realpath(path) if os.path.isabs(path) else os.path.realpath(os.path.join(snapshot, path))
    if real == snapshot or real.startswith(snapshot + os.sep):
        return os.path.relpath(real, snapshot)
    if share and real.startswith(share.rstrip("/") + "/"):
        return FRAMAC_SHARE_PREFIX + real[len(share.rstrip("/")) + 1 :]
    return path


def _deciding_prover(goal: dict) -> str | None:
    attempts = [attempt for attempt in (goal.get("provers") or []) if isinstance(attempt, dict)]
    winner = next((attempt.get("prover") for attempt in attempts if attempt.get("success")), None)
    return winner or (attempts[-1].get("prover") if attempts else None)


def goal_entry(goal: dict, snapshot: str, share: str | None) -> dict:
    """The gate's record of one report goal, with its file relative to the snapshot."""
    return {
        "goal": goal.get("goal"),
        "property": goal.get("property"),
        "kind": goal_kind(goal),
        "function": goal.get("function"),
        "behavior": goal.get("behavior"),
        "file": _snapshot_relative(goal.get("file"), snapshot, share),
        "line": goal.get("line"),
        "smoke": bool(goal.get("smoke")),
        "passed": bool(goal.get("passed")),
        "verdict": str(goal.get("verdict", NO_VERDICT)).lower(),
        "prover": _deciding_prover(goal),
    }


def is_goal_list(report: object) -> bool:
    return isinstance(report, list) and all(isinstance(g, dict) and "goal" in g and "passed" in g for g in report)


@dataclass
class GoalTally:
    entries: list[dict] = field(default_factory=list)
    by_verdict: dict[str, int] = field(default_factory=dict)
    smoke_total: int = 0
    smoke_failed: list[dict] = field(default_factory=list)
    invalid: list[dict] = field(default_factory=list)
    prover_failed: list[dict] = field(default_factory=list)
    unproved: list[dict] = field(default_factory=list)
    target_goals: list[dict] = field(default_factory=list)

    @property
    def smoke_passed(self) -> int:
        return self.smoke_total - len(self.smoke_failed)

    @property
    def non_smoke(self) -> int:
        return len(self.entries) - self.smoke_total

    def add(self, entry: dict, target: str) -> None:
        self.entries.append(entry)
        if entry["smoke"]:
            self._add_smoke_test(entry)
            return
        verdict = entry["verdict"]
        self.by_verdict[verdict] = self.by_verdict.get(verdict, 0) + 1
        if entry["function"] == target:
            self.target_goals.append(entry)
        if entry["passed"] and verdict == VALID:
            return
        if verdict == INVALID:
            self.invalid.append(entry)
        elif verdict == PROVER_FAILED:
            self.prover_failed.append(entry)
        else:
            self.unproved.append(entry)

    def _add_smoke_test(self, entry: dict) -> None:
        self.smoke_total += 1
        if not entry["passed"]:
            self.smoke_failed.append(entry)


def tally_goals(goals: list[dict], *, target: str, snapshot: str, share: str | None) -> GoalTally:
    tally = GoalTally()
    for goal in goals:
        tally.add(goal_entry(goal, snapshot, share), target)
    return tally


class ContractApplication(NamedTuple):
    applied: bool
    missing_clause: str


def contract_application(contracts: list[TargetContract], target_goals: list[dict]) -> ContractApplication:
    """Whether the target has goals for the approved ensures/assigns clauses, i.e. Frama-C saw the contract."""
    properties = [str(entry.get("property") or "") for entry in target_goals]
    wants_ensures = any(contract.clauses.get("ensures") for contract in contracts)
    wants_assigns = any(contract.clauses.get("assigns") for contract in contracts)
    if wants_ensures and not any(_ENSURES_PROPERTY_RE.search(prop) for prop in properties):
        return ContractApplication(False, "ensures")
    if wants_assigns and not any(_ASSIGNS_PROPERTY_RE.search(prop) for prop in properties):
        return ContractApplication(False, "assigns")
    return ContractApplication(True, "")
