from __future__ import annotations

import json

import pytest
from cbmc_replay_support import write_executable
from cprover_support import (
    GOTO_CC_PATH,
    GOTO_INSTRUMENT_PATH,
    MEASURED_GOTO_CC_VERSION,
    MEASURED_GOTO_INSTRUMENT_VERSION,
    MEASURED_GOTO_LD_VERSION,
    measured_toolchain,
    release_line,
    with_tool,
)

from leanc.cli import main
from leanc.pipeline.doctor import CPROVER_PIN_CHECK, inductive_tool_checks
from leanc.toolchain.cprover import cprover_pin
from leanc.toolchain.tools import GOTO_CC_ENV, GOTO_INSTRUMENT_ENV, tool_version

GOTO_CC_CHECK = "goto-cc (inductive gate)"
GOTO_INSTRUMENT_CHECK = "goto-instrument (inductive gate)"
INDUCTIVE_CHECKS = [GOTO_CC_CHECK, GOTO_INSTRUMENT_CHECK, CPROVER_PIN_CHECK]
PIN_FIX = (
    "the inductive gate gives ERROR unless cbmc, goto-cc and goto-instrument all report one tested release "
    "(cbmc-5.95.1)"
)


def _checks(tc) -> dict:
    return {check.name: check for check in inductive_tool_checks(cprover_pin(tc))}


def test_tested_tools_give_three_passing_optional_checks():
    checks = inductive_tool_checks(cprover_pin(measured_toolchain()))
    assert [check.name for check in checks] == INDUCTIVE_CHECKS
    assert all(check.ok and not check.required and check.fix == "" for check in checks)
    by_name = {check.name: check for check in checks}
    assert by_name[GOTO_CC_CHECK].detail == (
        f"{GOTO_CC_PATH}: {MEASURED_GOTO_CC_VERSION}; release cbmc-5.95.1 is tested for the inductive gate"
    )
    assert by_name[GOTO_INSTRUMENT_CHECK].detail == (
        f"{GOTO_INSTRUMENT_PATH}: {MEASURED_GOTO_INSTRUMENT_VERSION}; "
        "release cbmc-5.95.1 is tested for the inductive gate"
    )
    assert by_name[CPROVER_PIN_CHECK].detail == (
        "cbmc, goto-cc and goto-instrument report the tested release cbmc-5.95.1"
    )


def test_an_untested_goto_instrument_fails_its_row_and_the_pin_only():
    tc = with_tool(measured_toolchain(), "goto-instrument", path=GOTO_INSTRUMENT_PATH,
                   version=release_line("goto-instrument", "5.96.0"))
    checks = _checks(tc)
    assert checks[GOTO_CC_CHECK].ok
    row = checks[GOTO_INSTRUMENT_CHECK]
    assert not row.ok and not row.required
    assert row.detail.endswith("release cbmc-5.96.0 is not tested for the inductive gate")
    assert row.fix == "install CBMC 5.95.1 (it ships goto-cc and goto-instrument) or set LEANC_GOTO_INSTRUMENT"
    pin = checks[CPROVER_PIN_CHECK]
    assert not pin.ok and not pin.required and pin.fix == PIN_FIX
    assert pin.detail == (
        "goto-instrument: release cbmc-5.96.0 is not tested for the inductive gate; releases differ: "
        "cbmc cbmc-5.95.1, goto-cc cbmc-5.95.1, goto-instrument cbmc-5.96.0 (tested releases: cbmc-5.95.1)"
    )


def test_a_missing_goto_cc_names_its_override():
    checks = _checks(with_tool(measured_toolchain(), "goto-cc", path=None, version=None))
    row = checks[GOTO_CC_CHECK]
    assert (row.ok, row.required, row.detail) == (False, False, "not found")
    assert row.fix.endswith("or set LEANC_GOTO_CC")
    assert checks[GOTO_INSTRUMENT_CHECK].ok
    assert checks[CPROVER_PIN_CHECK].detail == "goto-cc: not found (tested releases: cbmc-5.95.1)"


def test_a_goto_cc_without_a_version_line_fails_its_row():
    row = _checks(with_tool(measured_toolchain(), "goto-cc", path=GOTO_CC_PATH, version=None))[GOTO_CC_CHECK]
    assert (row.ok, row.required) == (False, False)
    assert row.detail == f"{GOTO_CC_PATH}: -; the --version probe printed nothing"


def test_goto_cc_in_another_personality_fails_its_row():
    row = _checks(with_tool(measured_toolchain(), "goto-cc", path=GOTO_CC_PATH, version=MEASURED_GOTO_LD_VERSION))[
        GOTO_CC_CHECK
    ]
    assert not row.ok
    assert "does not start with 'gcc (goto-cc '" in row.detail


def test_an_untested_cbmc_fails_the_pin_but_not_the_goto_rows():
    checks = _checks(with_tool(measured_toolchain(), "cbmc", path="/x/cbmc", version=release_line("cbmc", "6.0.0")))
    assert checks[GOTO_CC_CHECK].ok and checks[GOTO_INSTRUMENT_CHECK].ok
    assert not checks[CPROVER_PIN_CHECK].ok
    assert checks[CPROVER_PIN_CHECK].detail.startswith("cbmc: release cbmc-6.0.0 is not tested")


@pytest.fixture
def _fresh_version_cache():
    tool_version.cache_clear()
    yield
    tool_version.cache_clear()


def test_doctor_reports_the_pin_without_failing_on_it(capsys, tmp_path, monkeypatch, needs_toolchain,
                                                      _fresh_version_cache):
    fake = write_executable(tmp_path / "goto-cc", "echo 'gcc (goto-cc 5.96.0 (cbmc-5.96.0)) 13.3.0'")
    monkeypatch.setenv(GOTO_CC_ENV, fake)
    monkeypatch.setenv(GOTO_INSTRUMENT_ENV, str(tmp_path / "absent-goto-instrument"))
    rc = main(["doctor", "--json"])
    data = json.loads(capsys.readouterr().out)
    checks = {check["name"]: check for check in data["checks"]}
    assert [name for name in checks if name in INDUCTIVE_CHECKS] == INDUCTIVE_CHECKS
    assert checks[GOTO_CC_CHECK]["detail"].startswith(f"{fake}: gcc (goto-cc 5.96.0 (cbmc-5.96.0)) 13.3.0; ")
    assert checks[GOTO_INSTRUMENT_CHECK]["detail"] == "not found"
    assert not any(checks[name]["ok"] or checks[name]["required"] for name in INDUCTIVE_CHECKS)
    assert data["ok"] is all(check["ok"] for check in data["checks"] if check["required"])
    assert rc == (0 if data["ok"] else 30)


def test_doctor_text_output_lists_the_inductive_rows(capsys, tmp_path, monkeypatch, needs_toolchain,
                                                      _fresh_version_cache):
    monkeypatch.setenv(GOTO_CC_ENV, str(tmp_path / "absent-goto-cc"))
    main(["doctor"])
    out = capsys.readouterr().out
    assert f"[warn] {GOTO_CC_CHECK} not found" in out
    assert "fix: install CBMC 5.95.1 (it ships goto-cc and goto-instrument) or set LEANC_GOTO_CC" in out
    assert f"[warn] {CPROVER_PIN_CHECK} goto-cc: not found (tested releases: cbmc-5.95.1)" in out
