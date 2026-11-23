from __future__ import annotations

import os
import shutil

import pytest
from cbmc_replay_support import write_executable
from cprover_support import (
    MEASURED_CBMC_VERSION,
    MEASURED_GOTO_CC_VERSION,
    MEASURED_GOTO_INSTRUMENT_VERSION,
    measured_toolchain,
    with_tool,
)

from leanc.foundation.sandbox import Sandbox
from leanc.toolchain.cprover import TESTED_INDUCTIVE_CBMC, cbmc_release, cprover_pin
from leanc.toolchain.environment import Toolchain, environment_info, toolchain
from leanc.toolchain.tools import (
    GOTO_CC_ENV,
    GOTO_INSTRUMENT_ENV,
    find_goto_cc,
    find_goto_instrument,
    tool_version,
)

FINDERS = [("goto-cc", GOTO_CC_ENV, find_goto_cc), ("goto-instrument", GOTO_INSTRUMENT_ENV, find_goto_instrument)]
NO_SANDBOX = Sandbox(kind="none", note="test")


def _fake(directory, name: str, line: str) -> str:
    return write_executable(directory / name, f"echo '{line}'")


@pytest.fixture(autouse=True)
def _fresh_version_cache():
    tool_version.cache_clear()
    yield
    tool_version.cache_clear()


def test_override_variables_are_named_after_the_tools():
    assert (GOTO_CC_ENV, GOTO_INSTRUMENT_ENV) == ("LEANC_GOTO_CC", "LEANC_GOTO_INSTRUMENT")


@pytest.mark.parametrize(("name", "env_var", "finder"), FINDERS)
def test_override_with_an_absolute_executable_wins(name, env_var, finder, tmp_path, monkeypatch):
    fake = _fake(tmp_path, f"my-{name}", "5.95.1 (cbmc-5.95.1)")
    monkeypatch.setenv(env_var, fake)
    assert finder() == fake


@pytest.mark.parametrize(("name", "env_var", "finder"), FINDERS)
def test_override_with_a_name_is_resolved_on_path(name, env_var, finder, tmp_path, monkeypatch):
    fake = _fake(tmp_path, f"my-{name}", "5.95.1 (cbmc-5.95.1)")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv(env_var, f"my-{name}")
    assert finder() == fake


@pytest.mark.parametrize(("name", "env_var", "finder"), FINDERS)
def test_a_broken_override_finds_nothing_and_never_falls_back(name, env_var, finder, tmp_path, monkeypatch):
    _fake(tmp_path, name, "5.95.1 (cbmc-5.95.1)")
    not_executable = tmp_path / f"plain-{name}"
    not_executable.write_text("#!/bin/sh\n")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    for broken in (str(tmp_path / "missing" / name), str(not_executable), "no-such-goto-tool"):
        monkeypatch.setenv(env_var, broken)
        assert finder() is None, broken


@pytest.mark.parametrize(("name", "env_var", "finder"), FINDERS)
def test_without_override_the_tool_is_found_on_path(name, env_var, finder, tmp_path, monkeypatch):
    fake = _fake(tmp_path, name, "5.95.1 (cbmc-5.95.1)")
    monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    assert finder() == fake


def test_toolchain_probes_the_goto_tools_found_by_the_overrides(tmp_path, monkeypatch):
    goto_cc = _fake(tmp_path, "goto-cc", MEASURED_GOTO_CC_VERSION)
    goto_instrument = _fake(tmp_path, "goto-instrument", MEASURED_GOTO_INSTRUMENT_VERSION)
    cbmc = _fake(tmp_path, "cbmc", MEASURED_CBMC_VERSION)
    monkeypatch.setenv(GOTO_CC_ENV, goto_cc)
    monkeypatch.setenv(GOTO_INSTRUMENT_ENV, goto_instrument)
    monkeypatch.setenv("LEANC_CBMC", cbmc)
    tc = toolchain()
    assert (tc.goto_cc, tc.goto_cc_version) == (goto_cc, MEASURED_GOTO_CC_VERSION)
    assert (tc.goto_instrument, tc.goto_instrument_version) == (goto_instrument, MEASURED_GOTO_INSTRUMENT_VERSION)
    assert (tc.cbmc, tc.cbmc_version) == (cbmc, MEASURED_CBMC_VERSION)
    assert cprover_pin(tc).tested


def test_toolchain_records_a_missing_goto_tool_as_none(tmp_path, monkeypatch):
    monkeypatch.setenv(GOTO_CC_ENV, str(tmp_path / "absent"))
    monkeypatch.setenv(GOTO_INSTRUMENT_ENV, str(tmp_path / "absent"))
    tc = toolchain()
    assert (tc.goto_cc, tc.goto_cc_version, tc.goto_instrument, tc.goto_instrument_version) == (None,) * 4
    assert not cprover_pin(tc).tested


def test_positional_toolchain_construction_keeps_working():
    tc = Toolchain("/x/clang", None, "clang version 18", None)
    assert (tc.goto_cc, tc.goto_cc_version, tc.goto_instrument, tc.goto_instrument_version) == (None,) * 4
    assert tc == Toolchain(clang="/x/clang", cbmc=None, clang_version="clang version 18", cbmc_version=None)


def _rows(tc: Toolchain) -> dict:
    return {row.name: row for row in environment_info(NO_SANDBOX, tc).tools}


def test_environment_info_lists_the_goto_tools():
    rows = _rows(measured_toolchain())
    names = list(rows)
    assert names.index("goto-cc") == names.index("cbmc") + 1
    assert names.index("goto-instrument") == names.index("cbmc") + 2
    tc = measured_toolchain()
    assert rows["goto-cc"].model_dump() == {
        "name": "goto-cc", "path": tc.goto_cc, "version": MEASURED_GOTO_CC_VERSION, "available": True, "note": None,
    }
    assert rows["goto-instrument"].model_dump() == {
        "name": "goto-instrument",
        "path": tc.goto_instrument,
        "version": MEASURED_GOTO_INSTRUMENT_VERSION,
        "available": True,
        "note": None,
    }


@pytest.mark.parametrize("present", ["goto-cc", "goto-instrument"])
def test_environment_info_marks_each_goto_tool_on_its_own(present):
    absent = "goto-instrument" if present == "goto-cc" else "goto-cc"
    tc = with_tool(measured_toolchain(), absent, path=None, version=None)
    rows = _rows(tc)
    assert rows[present].available and rows[present].path and rows[present].version
    assert (rows[absent].path, rows[absent].version, rows[absent].available) == (None, None, False)
    assert rows["cbmc"].available


def test_environment_info_marks_absent_goto_tools_unavailable():
    rows = _rows(Toolchain(clang=None, cbmc=None, clang_version=None, cbmc_version=None))
    for name in ("goto-cc", "goto-instrument"):
        assert (rows[name].path, rows[name].version, rows[name].available) == (None, None, False)


@pytest.fixture
def needs_cprover():
    if not all(shutil.which(name) for name in ("cbmc", "goto-cc", "goto-instrument")):
        pytest.skip("cbmc, goto-cc and goto-instrument not installed")


def test_installed_cprover_tools_print_one_release_token(needs_cprover, monkeypatch):
    for env_var in ("LEANC_CBMC", GOTO_CC_ENV, GOTO_INSTRUMENT_ENV):
        monkeypatch.delenv(env_var, raising=False)
    tc = toolchain()
    pin = cprover_pin(tc)
    releases = {release.release for release in pin.tools}
    assert len(releases) == 1 and None not in releases, [release.version_line for release in pin.tools]
    assert pin.goto_cc.problem is None or pin.goto_cc.release not in TESTED_INDUCTIVE_CBMC
    assert pin.tested == (cbmc_release(tc.cbmc_version) in TESTED_INDUCTIVE_CBMC)
