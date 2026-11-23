from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanc.backends.inductive.loops import DFCC_ARGS, GotoLoop, parse_loops, residual_loops

HARNESS = "harness/inductive.c"
LEANC_PACKAGE = Path(__file__).resolve().parent.parent / "leanc"
LOOP_CONTRACTS_FILE_FLAG = "--loop-contracts-file"
APPLY_LOOP_CONTRACTS_FLAG = "--apply-loop-contracts"
DFCC_FLAG = "--dfcc"


def listing(*loops: dict) -> str:
    return json.dumps([{"program": "goto-instrument"}, {"loops": list(loops)}, {"messageType": "STATUS-MESSAGE", "messageText": "Reading"}])


def loop(name: str, file: str | None, line: str = "11") -> dict:
    location = {"function": name.split(".")[0], "line": line}
    if file is not None:
        location["file"] = file
    return {"name": name, "sourceLocation": location}


def test_zero_loops_parse_to_an_empty_list():
    assert parse_loops(listing()) == []


def test_loops_carry_name_and_location():
    assert parse_loops(listing(loop("lower_bound.0", "src/iset.c"))) == [GotoLoop("lower_bound.0", "src/iset.c", 11, "lower_bound")]


@pytest.mark.parametrize(
    "stdout, expected",
    [
        ("[", "not valid JSON"),
        ("{}", "not a JSON list"),
        (json.dumps([{"program": "goto-instrument"}]), "expected one loops list"),
        (json.dumps([{"loops": {}}]), "expected one loops list"),
        (json.dumps([{"loops": []}, {"loops": []}]), "expected one loops list"),
        (json.dumps([{"loops": [{"sourceLocation": {}}]}]), "has no name"),
        (json.dumps([{"messageType": "ERROR", "messageText": "bad"}, {"loops": []}]), "reported errors"),
    ],
)
def test_unusable_loop_listings_are_reported(stdout, expected):
    result = parse_loops(stdout)
    assert isinstance(result, str) and expected in result


def test_residual_loops_exclude_library_and_harness_only():
    loops = [
        GotoLoop("__CPROVER_contracts_write_set_check_assigns_clause_inclusion.0", "<builtin-library-__CPROVER_contracts_library>", 900, None),
        GotoLoop("main.0", HARNESS, 12, "main"),
        GotoLoop("main.1", "./harness/inductive.c", 14, "main"),
        GotoLoop("tally_clear.0", "src/tally.c", 33, "tally_clear"),
        GotoLoop("helper.0", "include/tally.h", 5, "helper"),
        GotoLoop("anonymous.0", None, None, None),
    ]
    assert [item.name for item in residual_loops(loops, HARNESS)] == ["tally_clear.0", "helper.0", "anonymous.0"]


def test_residual_loop_description_names_location_and_cbmc_loop():
    assert GotoLoop("tally_clear.0", "src/tally.c", 33, "tally_clear").describe() == "src/tally.c:33 (CBMC loop tally_clear.0)"


def test_dfcc_arguments_apply_loop_contracts_only_with_dfcc_main():
    assert DFCC_ARGS == (DFCC_FLAG, "main", APPLY_LOOP_CONTRACTS_FLAG)


def _python_sources() -> list[Path]:
    return sorted(LEANC_PACKAGE.rglob("*.py"))


def test_no_leanc_module_uses_the_loop_contracts_file_option():
    assert [path for path in _python_sources() if LOOP_CONTRACTS_FILE_FLAG in path.read_text(encoding="utf-8")] == []


def test_apply_loop_contracts_appears_only_next_to_dfcc():
    offenders = []
    for path in _python_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if APPLY_LOOP_CONTRACTS_FLAG in line and DFCC_FLAG not in line:
                offenders.append(f"{path.relative_to(LEANC_PACKAGE)}:{number}")
    assert offenders == []
