from __future__ import annotations

import os
from dataclasses import dataclass, field

from leanc.backends.cbmc.output import PROPERTY_HOLDS, PROPERTY_VIOLATED
from leanc.backends.counterexample.locations import location_from_cbmc
from leanc.backends.counterexample.properties import HARNESS_ASSERTION_CLASS, is_unwinding_property, property_class

HARNESS_ENTRY_POINT = "main"


@dataclass
class PropertyTally:
    violated: list[dict] = field(default_factory=list)
    unwinding_failed: list[dict] = field(default_factory=list)
    undecided: list[dict] = field(default_factory=list)
    checked: int = 0
    holding: int = 0
    unwinding_total: int = 0


def _status(result: dict) -> str:
    return str(result.get("status", "")).upper()


def _tally_unwinding_assertion(tally: PropertyTally, result: dict) -> None:
    tally.unwinding_total += 1
    status = _status(result)
    if status == PROPERTY_VIOLATED:
        tally.unwinding_failed.append(result)
    elif status != PROPERTY_HOLDS:
        tally.undecided.append(result)


def _tally_real_property(tally: PropertyTally, result: dict) -> None:
    tally.checked += 1
    status = _status(result)
    if status == PROPERTY_VIOLATED:
        tally.violated.append(result)
    elif status == PROPERTY_HOLDS:
        tally.holding += 1
    else:
        tally.undecided.append(result)


def tally_properties(results: list[dict]) -> PropertyTally:
    tally = PropertyTally()
    for result in results:
        if is_unwinding_property(result):
            _tally_unwinding_assertion(tally, result)
        else:
            _tally_real_property(tally, result)
    return tally


def _same_path(first: str | None, second: str | None) -> bool:
    if not first or not second:
        return False
    return os.path.normpath(first).removeprefix("./") == os.path.normpath(second).removeprefix("./")


def count_listed_harness_assertions(results: list[dict], harness: str) -> int:
    listed = 0
    for result in results:
        location = location_from_cbmc(result.get("sourceLocation"))
        if (
            location is not None
            and location.function == HARNESS_ENTRY_POINT
            and _same_path(location.file, harness)
            and property_class(result.get("property"), result.get("description")) == HARNESS_ASSERTION_CLASS
        ):
            listed += 1
    return listed
