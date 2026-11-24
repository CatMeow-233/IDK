from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence

from leanc.backends.cbmc.bodiless import bodiless_function, is_modelled_bodiless
from leanc.backends.cbmc.harness import count_harness_assertions
from leanc.backends.cbmc.output import PROPERTY_HOLDS, CbmcOutput
from leanc.backends.cbmc.properties import PropertyTally, count_listed_harness_assertions
from leanc.backends.counterexample.locations import location_from_cbmc
from leanc.backends.counterexample.properties import property_class
from leanc.backends.inductive.harness_calls import ASSUME_AFTER_CLAIMED_CALL_LABEL, CLAIMED_CALL_LABEL_PREFIX
from leanc.backends.inductive.problems import BuildProblem, inconclusive, tool_error
from leanc.foundation.reasons import Reason

INSERTED_CHECK_PREFIX = "leanc_"
NOTHING_CHECKED = "CBMC generated no property to check for the inductive harness and code; nothing was verified"
MAX_LISTED = 5


def is_harness_rule_check(result: dict) -> bool:
    description = str(result.get("description") or "")
    return description.startswith(CLAIMED_CALL_LABEL_PREFIX) or description == ASSUME_AFTER_CLAIMED_CALL_LABEL


def _where(result: dict) -> str:
    location = location_from_cbmc(result.get("sourceLocation"))
    return location.short() if location else "?"


def harness_rule_problem(results: Sequence[dict]) -> BuildProblem | None:
    broken = [
        f"{_where(result)}: {result.get('description')} is {result.get('status')}"
        for result in results
        if is_harness_rule_check(result) and str(result.get("status", "")).upper() != PROPERTY_HOLDS
    ]
    if not broken:
        return None
    return tool_error(
        "the inductive harness breaks its structural rules on some path (a second claimed call after an earlier "
        "one, or an assumption after a claimed call); each path may make one claimed call, after every assumption",
        broken,
    )


def undecided_problem(tally: PropertyTally) -> BuildProblem | None:
    if not tally.undecided:
        return None
    return tool_error(
        f"CBMC could not decide {len(tally.undecided)} property(ies) of the inductive verify run",
        [f"{result.get('property')} ({result.get('status')})" for result in tally.undecided[:MAX_LISTED]],
    )


def nothing_checked_problem(tally: PropertyTally) -> BuildProblem | None:
    if tally.checked > 0:
        return None
    return inconclusive(Reason.GATE_INCONCLUSIVE, NOTHING_CHECKED)


def _harness_own(results: Sequence[dict]) -> list[dict]:
    return [result for result in results if not str(result.get("description") or "").startswith(INSERTED_CHECK_PREFIX)]


def harness_assertion_counts(results: Sequence[dict], harness_text: str, harness: str) -> tuple[int, int]:
    return count_harness_assertions(harness_text), count_listed_harness_assertions(_harness_own(results), harness)


def missing_assertions_problem(in_source: int, listed: int, harness: str) -> BuildProblem | None:
    if listed >= in_source:
        return None
    return tool_error(
        f"harness assertions missing from CBMC's property list (assert machinery overridden?): {harness} main() "
        f"contains {in_source} assertion(s), CBMC checked {listed}; no verdict is possible"
    )


def property_class_counts(results: Iterable[dict]) -> dict[str, int]:
    counts = Counter(property_class(result.get("property"), result.get("description")) for result in results)
    return dict(sorted(counts.items()))


def unmodelled_functions(output: CbmcOutput, after_dfcc: Iterable[str]) -> list[str]:
    found = list(dict.fromkeys(after_dfcc))
    for warning in output.warnings:
        function = bodiless_function(str(warning.get("messageText", "")))
        if function is not None and not is_modelled_bodiless(function) and function not in found:
            found.append(function)
    return found
