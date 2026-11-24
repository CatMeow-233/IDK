from __future__ import annotations

import json
from typing import Any

from leanc.backends.cbmc.symbol_table import cprover_declarations, parse_symbol_table
from leanc.backends.cbmc.vacuity_divergence import ReachSite
from leanc.backends.cbmc.vacuity_sites import unreported_sites
from leanc.rules.loop_sites import Origin

HARNESS = "harness/h.c"
BUILT_IN = "<built-in-additions>"


def entry(base: str, file: str | None) -> dict[str, Any]:
    location = {"id": "", "namedSub": {"file": {"id": file}, "line": {"id": "7"}}} if file else {"id": ""}
    return {"baseName": base, "module": "h", "location": location, "type": {"id": "code"}, "value": {"id": "nil"}}


def table(entries: dict[str, dict[str, Any]]):
    parsed = parse_symbol_table(json.dumps([{"program": "CBMC 5.95.1"}, {"symbolTable": entries}]))
    assert isinstance(parsed, dict), parsed
    return parsed


def test_a_cprover_name_declared_in_a_real_file_is_listed_with_its_location():
    symbols = table(
        {
            "arm::__CPROVER_assert": entry("__CPROVER_assert", HARNESS),
            "main::1::__CPROVER_assume": entry("__CPROVER_assume", "include/h.h"),
            "__CPROVER_assert$link1": entry("other", HARNESS),
            "keep::x::__CPROVER_tmp": entry("tmp", HARNESS),
        }
    )
    assert cprover_declarations(symbols) == [
        f"{HARNESS}:7: __CPROVER_assert$link1",
        f"{HARNESS}:7: arm::__CPROVER_assert",
        f"{HARNESS}:7: keep::x::__CPROVER_tmp",
        "include/h.h:7: main::1::__CPROVER_assume",
    ]


def test_built_in_and_unlocated_cprover_symbols_and_plain_names_are_not_listed():
    symbols = table(
        {
            "__CPROVER_assert": entry("__CPROVER_assert", BUILT_IN),
            "__CPROVER__start": entry("__CPROVER__start", None),
            "main": entry("main", HARNESS),
            "main::1::leanc_cprover": entry("leanc_cprover", HARNESS),
        }
    )
    assert cprover_declarations(symbols) == []


def probe(label: str, line: int, file: str = HARNESS) -> dict[str, Any]:
    location = {"file": file, "line": str(line), "function": "main"}
    return {"property": f"main.assertion.{line}", "description": f"leanc_reach:{label}", "status": "FAILURE", "sourceLocation": location}


def site(label: str, line: int, file: str = HARNESS) -> ReachSite:
    return ReachSite(Origin(file, line), label)


def test_each_site_needs_its_own_reported_probe_at_its_file_line_and_label():
    results = [probe("a", 4), probe("a", 4), probe("b", 9, "./harness/h.c")]
    sites = [site("a", 4), site("a", 4), site("a", 4), site("b", 9), site("b", 10), site("c", 9)]
    assert unreported_sites(results, sites, []) == [site("a", 4), site("b", 10), site("c", 9)]


def test_a_site_whose_label_is_already_flagged_is_not_reported_twice():
    assert unreported_sites([], [site("a", 4), site("b", 5)], ["a"]) == [site("b", 5)]


def test_a_site_without_an_origin_is_unreported():
    assert unreported_sites([probe("a", 4)], [ReachSite(None, "a")], []) == [ReachSite(None, "a")]
