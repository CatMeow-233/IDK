from __future__ import annotations

import json
from typing import Any

import pytest

from leanc.backends.inductive.harness_calls import ReturnKind
from leanc.backends.cbmc.symbol_table import SymbolTable, parse_symbol_table
from leanc.backends.inductive.symbols import (
    RENAME_REMEDY,
    capture_problems,
    claimed_problems,
    return_kinds,
    trusted_definitions,
)

HARNESS = "harness/inductive.c"
EDITABLE = "src/tally.c"
HARNESS_MODULE = "trusted_1_harness"
EDITABLE_MODULE = "editable_00"


def symbol(
    module: str,
    file: str | None,
    line: str = "1",
    *,
    base: str | None = None,
    value: str = "compiled",
    local: bool = False,
    static: bool = False,
    extern: bool = False,
    is_type: bool = False,
    type_id: str = "code",
    returns: str | None = "signedbv",
) -> dict[str, Any]:
    location = {"id": "", "namedSub": {"file": {"id": file}, "line": {"id": line}}} if file else {"id": ""}
    symbol_type: dict[str, Any] = {"id": type_id}
    if returns is not None:
        symbol_type["namedSub"] = {"return_type": {"id": returns}}
    entry = {
        "module": module,
        "location": location,
        "value": {"id": value},
        "isFileLocal": local,
        "isStaticLifetime": static,
        "isExtern": extern,
        "isType": is_type,
        "type": symbol_type,
    }
    if base is not None:
        entry["baseName"] = base
    return entry


def output(table: dict[str, dict[str, Any]], *extra: dict[str, Any]) -> str:
    named = {name: {"baseName": entry.get("baseName", name.split("::")[-1].split("$")[0]), **entry} for name, entry in table.items()}
    return json.dumps([{"program": "goto-instrument"}, *extra, {"symbolTable": named}])


def parsed(table: dict[str, dict[str, Any]]) -> SymbolTable:
    result = parse_symbol_table(output(table))
    assert not isinstance(result, str), result
    return result


HARNESS_ALONE = {
    "main": symbol(HARNESS_MODULE, HARNESS, "7"),
    "helper": symbol(HARNESS_MODULE, HARNESS, "3"),
    "leanc_claimed_calls": symbol(HARNESS_MODULE, HARNESS, "1", value="nil", local=True, static=True, type_id="signedbv", returns=None),
    "tally_below": symbol(HARNESS_MODULE, "include/tally.h", "4", value="nil"),
    "__CPROVER_rounding_mode": symbol(HARNESS_MODULE, "<built-in-additions>", value="constant", static=True),
    "main::1::s": symbol(HARNESS_MODULE, HARNESS, "9", value="nil", type_id="signedbv", returns=None),
    "tag-state": symbol(HARNESS_MODULE, HARNESS, "2", value="nil", is_type=True, type_id="struct", returns=None),
    "external": symbol(HARNESS_MODULE, HARNESS, "4", value="nil", static=True, extern=True, type_id="signedbv", returns=None),
}
LINKED = {
    **HARNESS_ALONE,
    "tally_below": symbol(HARNESS_MODULE, EDITABLE, "3"),
    "tally_clear": symbol(EDITABLE_MODULE, EDITABLE, "27", returns="empty"),
}


def test_parse_rejects_unusable_output():
    assert "not valid JSON" in parse_symbol_table("{")
    assert "not a JSON list" in parse_symbol_table("{}")
    assert "expected one symbolTable" in parse_symbol_table(json.dumps([{"program": "goto-instrument"}]))
    error = {"messageType": "ERROR", "messageText": "failed to read"}
    assert "reported errors" in parse_symbol_table(output({}, error))
    assert "is not a JSON object" in parse_symbol_table(json.dumps([{"symbolTable": {"x": 3}}]))


def test_trusted_definitions_keep_only_named_definitions_of_the_unit():
    definitions = trusted_definitions(parsed(HARNESS_ALONE), HARNESS_MODULE)
    assert sorted(definitions) == ["helper", "leanc_claimed_calls", "main"]


def test_trusted_definitions_ignore_other_modules():
    assert trusted_definitions(parsed(HARNESS_ALONE), EDITABLE_MODULE) == {}


def test_clean_link_has_no_capture_problem():
    trusted = trusted_definitions(parsed(HARNESS_ALONE), HARNESS_MODULE)
    assert capture_problems(parsed(LINKED), [trusted], [EDITABLE]) == []


def test_link_rename_of_a_trusted_name_is_a_problem():
    trusted = trusted_definitions(parsed(HARNESS_ALONE), HARNESS_MODULE)
    linked = {**LINKED, "helper$link1": symbol(EDITABLE_MODULE, EDITABLE, "3", base="helper", local=True)}
    problems = capture_problems(parsed(linked), [trusted], [EDITABLE])
    assert len(problems) == 1
    assert "helper$link1" in problems[0] and "link-time rename" in problems[0]


def test_a_static_editable_twin_of_a_static_harness_function_stays_a_problem():
    harness = {**HARNESS_ALONE, "helper": symbol(HARNESS_MODULE, HARNESS, "3", local=True)}
    trusted = trusted_definitions(parsed(harness), HARNESS_MODULE)
    linked = {**harness, "tally_below": LINKED["tally_below"], "helper$link1": symbol(EDITABLE_MODULE, EDITABLE, "3", base="helper", local=True)}
    problems = capture_problems(parsed(linked), [trusted], [EDITABLE])
    assert len(problems) == 1 and "helper$link1" in problems[0]
    assert RENAME_REMEDY in problems[0]


def test_trusted_name_bound_to_an_editable_definition_is_a_problem():
    trusted = trusted_definitions(parsed(HARNESS_ALONE), HARNESS_MODULE)
    linked = {
        **LINKED,
        "helper": symbol(EDITABLE_MODULE, EDITABLE, "3", local=True),
        "helper$link1": symbol(HARNESS_MODULE, HARNESS, "3", base="helper"),
    }
    problems = capture_problems(parsed(linked), [trusted], [EDITABLE])
    assert any(f"is bound to a definition at {EDITABLE}:3" in problem for problem in problems)
    assert any("helper$link1" in problem for problem in problems)


@pytest.mark.parametrize(
    "rebound, shown",
    [
        (symbol(EDITABLE_MODULE, EDITABLE, "3"), f"{EDITABLE}:3"),
        (symbol(HARNESS_MODULE, HARNESS, "4"), f"{HARNESS}:4"),
        (symbol(HARNESS_MODULE, HARNESS, "3", local=True), f"{HARNESS}:3"),
        (symbol(HARNESS_MODULE, HARNESS, "3", value="nil"), f"{HARNESS}:3"),
    ],
    ids=["other_file", "other_line", "file_local_flipped", "definition_dropped"],
)
def test_trusted_name_bound_to_any_other_definition_is_exactly_one_problem(rebound, shown):
    trusted = trusted_definitions(parsed(HARNESS_ALONE), HARNESS_MODULE)
    problems = capture_problems(parsed({**LINKED, "helper": rebound}), [trusted], [EDITABLE])
    assert problems == [f"trusted symbol helper defined at {HARNESS}:3 is bound to a definition at {shown} after linking"]


def test_missing_trusted_symbol_is_a_problem():
    trusted = trusted_definitions(parsed(HARNESS_ALONE), HARNESS_MODULE)
    linked = {name: entry for name, entry in LINKED.items() if name != "helper"}
    assert any("missing after linking" in problem for problem in capture_problems(parsed(linked), [trusted], [EDITABLE]))


@pytest.mark.parametrize("name", ["leanc_hook", "tally_below::1::leanc_tmp", "LEANC_SHADOW"])
def test_editable_reserved_names_are_a_problem(name):
    linked = {**LINKED, name: symbol(EDITABLE_MODULE, EDITABLE, "5", local=True)}
    problems = capture_problems(parsed(linked), [], [EDITABLE])
    assert len(problems) == 1
    assert "must not define names starting with leanc_" in problems[0]


def test_reserved_names_outside_editable_files_are_allowed():
    assert capture_problems(parsed(LINKED), [], [EDITABLE]) == []


def test_claimed_functions_must_be_global_editable_definitions():
    linked = {
        **LINKED,
        "static_one": symbol(EDITABLE_MODULE, EDITABLE, "8", local=True),
        "trusted_one": symbol(HARNESS_MODULE, HARNESS, "8"),
        "declared_only": symbol(HARNESS_MODULE, "include/tally.h", "5", value="nil"),
    }
    names = ["tally_below", "static_one", "trusted_one", "declared_only", "absent"]
    problems = claimed_problems(parsed(linked), names, [EDITABLE], HARNESS)
    assert [problem.split(" ")[2] for problem in problems] == ["static_one", "trusted_one", "declared_only", "absent"]


def test_main_must_come_from_the_harness():
    linked = {**LINKED, "main": symbol(EDITABLE_MODULE, EDITABLE, "40")}
    problems = claimed_problems(parsed(linked), ["tally_below"], [EDITABLE], HARNESS)
    assert problems == [f"main() must be defined by the harness {HARNESS}, found {EDITABLE}:40"]


def test_return_kinds_distinguish_void():
    kinds = return_kinds(parsed(LINKED), ["tally_below", "tally_clear"])
    assert kinds == {"tally_below": ReturnKind.VALUE, "tally_clear": ReturnKind.VOID}


def test_unreadable_return_type_is_reported():
    linked = {**LINKED, "tally_below": symbol(HARNESS_MODULE, EDITABLE, "3", returns=None)}
    assert "not readable" in return_kinds(parsed(linked), ["tally_below"])
    assert "not readable" in return_kinds(parsed(LINKED), ["absent"])
