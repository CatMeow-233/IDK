"""Failure reports and diagnostics for WP goals that are refuted, unproved or vacuous."""

from __future__ import annotations

import os
import re
from pathlib import Path

from leanc.backends.wp.goals import SMOKE_PREFIX, goal_property
from leanc.foundation.models import Diagnostic, FailureReport, Location
from leanc.rules.acsl import TargetContract

TOOL = "frama-c-wp"
MAX_QUOTED_SOURCE_CHARS = 160
ELLIPSIS = "..."
DIAGNOSTIC_RULE_BY_FAILURE_KIND = {"unproved": "unproved", "vacuity": "smoke-test", "counterexample": "invalid"}

_ACSL_OPENING_RE = re.compile(r"^(?:/\*@|//@|@)\s*")
_COMMENT_CLOSING_RE = re.compile(r"\s*\*/$")

SMOKE_TEST_EXPLANATION = (
    "WP smoke tests try to prove 'false' from the hypotheses at a program point; here a prover succeeded, "
    "so every property proved there holds vacuously. An inconsistent requires / assumes is a defect of the "
    "approved contract (fixing it needs a re-approved spec); dead code or an inconsistent loop invariant is "
    "a defect of the editable code or its annotations."
)


class SourceLines:
    """Snapshot source lines quoted in descriptions instead of line numbers, so fingerprints survive edits."""

    def __init__(self, root: Path):
        self.root = root
        self._cache: dict[str, list[str]] = {}

    def _lines(self, rel: str) -> list[str]:
        if rel not in self._cache:
            path = self.root / rel
            self._cache[rel] = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []
        return self._cache[rel]

    def quote(self, rel: str | None, line: int | None) -> str:
        if not rel or not line or os.path.isabs(rel) or rel.startswith(".."):
            return ""
        lines = self._lines(rel)
        if not 1 <= line <= len(lines):
            return ""
        text = " ".join(lines[line - 1].split())
        text = _COMMENT_CLOSING_RE.sub("", _ACSL_OPENING_RE.sub("", text)) or text
        if len(text) <= MAX_QUOTED_SOURCE_CHARS:
            return text
        return text[: MAX_QUOTED_SOURCE_CHARS - len(ELLIPSIS)] + ELLIPSIS


def _goal_location(entry: dict) -> Location:
    return Location(file=entry["file"], line=entry["line"], function=entry["function"])


def refuted_goal_failure(entry: dict, lines: SourceLines, report_ref: str | None) -> FailureReport:
    text = lines.quote(entry["file"], entry["line"])
    return FailureReport(
        id=f"wp:{entry['goal']}",
        kind="counterexample",
        tool=TOOL,
        property_id=entry["goal"],
        description=f"{entry['kind']} refuted: `{text}`" if text else f"{entry['kind']} refuted",
        location=_goal_location(entry),
        explanation=f"a prover reported the goal {entry['goal']} as INVALID: the code violates this property for "
        "some input allowed by the approved preconditions.",
        raw_ref=report_ref,
    )


def unproved_goal_failure(
    entry: dict, lines: SourceLines, report_ref: str | None, provers_tried: str, prover_timeout_s: int
) -> FailureReport:
    text = lines.quote(entry["file"], entry["line"])
    behavior = f" (behavior {entry['behavior']})" if entry.get("behavior") else ""
    return FailureReport(
        id=f"wp:{entry['goal']}",
        kind="unproved",
        tool=TOOL,
        property_id=entry["goal"],
        description=(
            f"{entry['kind']}{behavior} not proved: `{text}`" if text else f"{entry['kind']}{behavior} not proved"
        ),
        location=_goal_location(entry),
        explanation=(
            f"Frama-C/WP could not prove {entry['goal']} with {provers_tried} ({prover_timeout_s} s per goal; "
            f"verdict: {entry['verdict']}). Unproved is not refuted: the code may be correct but need proof "
            "annotations (loop invariants covering every variable the loop changes, loop assigns, helper "
            "contracts), or it may really violate the property for some input allowed by the preconditions."
        ),
        raw_ref=report_ref,
    )


def _is_unreachable_behavior(prop: str, behavior: str | None) -> bool:
    return bool(prop.startswith(SMOKE_PREFIX) and prop.endswith("_assumes") or (behavior and "assumes" in prop))


def _smoke_test_description(prop: str, behavior: str | None, function: str, shown: str) -> str:
    if prop.startswith("wp_smoke_default_requires"):
        return f"inconsistent precondition: the requires clauses of {function}() can never hold together"
    if _is_unreachable_behavior(prop, behavior):
        return f"behavior {behavior or '?'} of {function}() can never apply (its assumes contradicts the requires)"
    if "dead_code" in prop:
        return f"dead code in {function}(): unreachable under the preconditions{shown}"
    if "dead_loop" in prop:
        return f"inconsistent loop invariant in {function}(): the loop body is unreachable{shown}"
    if "dead_call" in prop:
        return f"a call in {function}() never returns according to the callee's contract{shown}"
    return f"smoke test {prop} of {function}() failed{shown}"


def smoke_test_failure(entry: dict, lines: SourceLines, target: str, report_ref: str | None) -> FailureReport:
    function = entry.get("function") or target
    text = lines.quote(entry["file"], entry["line"])
    shown = f": `{text}`" if text else ""
    return FailureReport(
        id=f"wp:smoke:{entry['goal']}",
        kind="vacuity",
        tool=TOOL,
        property_id=entry["goal"],
        description=_smoke_test_description(goal_property(entry), entry.get("behavior"), function, shown),
        location=Location(file=entry["file"], line=entry["line"], function=function),
        explanation=SMOKE_TEST_EXPLANATION,
        raw_ref=report_ref,
    )


def contract_not_applied_failure(
    contracts: list[TargetContract], target: str, missing_clause: str, report_ref: str | None
) -> FailureReport:
    return FailureReport(
        id="wp:contract-not-applied",
        kind="unproved",
        tool=TOOL,
        property_id="contract-not-applied",
        description=f"the approved contract of {target}() produced no {missing_clause} proof obligation",
        location=Location(file=contracts[0].file, line=contracts[0].line, function=target),
        explanation="Frama-C did not attach the approved ACSL contract to the definition of the target (is the "
        "protected header included by the editable source that defines it?). Without it nothing about the "
        "specified behaviour is proved.",
        raw_ref=report_ref,
    )


def failure_diagnostic(failure: FailureReport) -> Diagnostic:
    return Diagnostic(
        tool=TOOL,
        rule_id=DIAGNOSTIC_RULE_BY_FAILURE_KIND[failure.kind],
        severity="error" if failure.kind == "counterexample" else "warning",
        message=f"{failure.description} [{failure.property_id}]",
        location=failure.location,
    )
