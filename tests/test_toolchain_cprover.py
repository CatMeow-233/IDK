from __future__ import annotations

import pytest
from cprover_support import (
    CBMC_PATH,
    GOTO_CC_PATH,
    GOTO_INSTRUMENT_PATH,
    MEASURED_CBMC_VERSION,
    MEASURED_GOTO_AS_VERSION,
    MEASURED_GOTO_CC_VERSION,
    MEASURED_GOTO_GCC_VERSION,
    MEASURED_GOTO_INSTRUMENT_VERSION,
    MEASURED_GOTO_LD_VERSION,
    measured_toolchain,
    release_line,
    with_tool,
)

from leanc.toolchain import cprover
from leanc.toolchain.cprover import (
    TESTED_INDUCTIVE_CBMC,
    CproverTool,
    PinProblem,
    cbmc_release,
    cprover_pin,
    pinned_releases_text,
)
from leanc.toolchain.environment import Toolchain

TOOLS = [tool.value for tool in CproverTool]
TWO_TESTED_RELEASES = ("5.95.1", "5.96.0")


def _release_of(pin, tool: str):
    return {release.tool.value: release for release in pin.tools}[tool]


def test_tested_release_is_the_measured_one():
    assert TESTED_INDUCTIVE_CBMC == ("5.95.1",)
    assert pinned_releases_text() == "cbmc-5.95.1"


@pytest.mark.parametrize(
    "line", [MEASURED_CBMC_VERSION, MEASURED_GOTO_INSTRUMENT_VERSION, MEASURED_GOTO_CC_VERSION]
)
def test_release_token_of_the_measured_version_lines(line):
    assert cbmc_release(line) == "5.95.1"


@pytest.mark.parametrize(
    ("line", "release"),
    [
        ("5.95.10 (cbmc-5.95.10)", "5.95.10"),
        ("5.95.1 (cbmc-5.95.1-3-gabc1234)", "5.95.1-3-gabc1234"),
        ("5.95 (cbmc-5.95)", "5.95"),
        ("6.0.0 (cbmc-6.0.0)", "6.0.0"),
        ("5.95.1 cbmc-5.95.1", None),
        ("5.95.1", None),
        ("", None),
        (None, None),
        ("(cbmc-)", None),
        ("(cbmc- 5.95.1)", None),
        ("(CBMC-5.95.1)", None),
        ("(cbmc-5.95.1 )", None),
        ("(cbmc-5.95.1) (cbmc-5.95.1)", None),
        ("(cbmc-5.95.1) (cbmc-6.0.0)", None),
    ],
)
def test_release_token_is_read_exactly(line, release):
    assert cbmc_release(line) == release


def test_measured_toolchain_is_tested():
    pin = cprover_pin(measured_toolchain())
    assert pin.tested
    assert pin.problems() == ()
    assert [release.problem for release in pin.tools] == [None, None, None]
    assert [release.path for release in pin.tools] == [CBMC_PATH, GOTO_CC_PATH, GOTO_INSTRUMENT_PATH]
    assert pin.describe() == "cbmc, goto-cc and goto-instrument report the tested release cbmc-5.95.1"
    assert _release_of(pin, "goto-cc").describe() == "goto-cc: release cbmc-5.95.1 is tested for the inductive gate"


def _defects(tool: str) -> list[tuple[str | None, str | None, PinProblem, str]]:
    path = {"cbmc": CBMC_PATH, "goto-cc": GOTO_CC_PATH, "goto-instrument": GOTO_INSTRUMENT_PATH}[tool]
    return [
        (None, release_line(tool, "5.95.1"), PinProblem.NOT_FOUND, f"{tool}: not found"),
        ("", release_line(tool, "5.95.1"), PinProblem.NOT_FOUND, f"{tool}: not found"),
        (path, None, PinProblem.NO_VERSION_LINE, f"{tool}: the --version probe printed nothing"),
        (path, "", PinProblem.NO_VERSION_LINE, f"{tool}: the --version probe printed nothing"),
        (path, "gcc 13.3.0" if tool == "goto-cc" else "5.95.1", PinProblem.NO_RELEASE_TOKEN, "no single"),
        (path, release_line(tool, "5.96.0"), PinProblem.UNTESTED_RELEASE, "release cbmc-5.96.0 is not tested"),
        (path, release_line(tool, "5.95.10"), PinProblem.UNTESTED_RELEASE, "release cbmc-5.95.10 is not tested"),
        (path, release_line(tool, "5.95.1-3-gabc"), PinProblem.UNTESTED_RELEASE, "cbmc-5.95.1-3-gabc is not"),
    ]


@pytest.mark.parametrize(
    ("tool", "path", "line", "problem", "text"),
    [(tool, *defect) for tool in TOOLS for defect in _defects(tool)],
)
def test_each_tool_is_pinned_on_its_own(tool, path, line, problem, text):
    pin = cprover_pin(with_tool(measured_toolchain(), tool, path=path, version=line))
    assert not pin.tested
    assert _release_of(pin, tool).problem is problem
    assert [release.problem is None for release in pin.tools] == [name != tool for name in TOOLS]
    assert any(entry.startswith(f"{tool}: ") and text in entry for entry in pin.problems())
    assert text in pin.describe() and pin.describe().endswith("(tested releases: cbmc-5.95.1)")


@pytest.mark.parametrize("line", [MEASURED_GOTO_LD_VERSION, MEASURED_GOTO_AS_VERSION])
def test_goto_cc_must_run_in_its_gcc_personality(line):
    assert cbmc_release(line) == "5.95.1"
    pin = cprover_pin(with_tool(measured_toolchain(), "goto-cc", path=GOTO_CC_PATH, version=line))
    assert _release_of(pin, "goto-cc").problem is PinProblem.NOT_GCC_PERSONALITY
    assert not pin.tested
    assert "does not start with 'gcc (goto-cc '" in pin.describe()


def test_the_gcc_personality_prefix_must_open_the_goto_cc_line():
    line = "x " + MEASURED_GOTO_CC_VERSION
    pin = cprover_pin(with_tool(measured_toolchain(), "goto-cc", path=GOTO_CC_PATH, version=line))
    assert _release_of(pin, "goto-cc").problem is PinProblem.NOT_GCC_PERSONALITY
    assert not pin.tested


def test_goto_cc_run_as_plain_gcc_has_no_release_token():
    pin = cprover_pin(with_tool(measured_toolchain(), "goto-cc", path=GOTO_CC_PATH, version=MEASURED_GOTO_GCC_VERSION))
    assert _release_of(pin, "goto-cc").problem is PinProblem.NO_RELEASE_TOKEN
    assert not pin.tested


@pytest.mark.parametrize("tool", ["cbmc", "goto-instrument"])
def test_the_gcc_personality_rule_is_only_for_goto_cc(tool):
    pin = cprover_pin(measured_toolchain())
    assert not _release_of(pin, tool).version_line.startswith(cprover.GOTO_CC_GCC_PERSONALITY_PREFIX)
    assert _release_of(pin, tool).problem is None


def test_the_tested_releases_are_read_when_the_pin_is_checked(monkeypatch):
    tc = measured_toolchain()
    for tool in TOOLS:
        tc = with_tool(tc, tool, path=f"/x/{tool}", version=release_line(tool, "5.96.0"))
    assert not cprover_pin(tc).tested
    monkeypatch.setattr(cprover, "TESTED_INDUCTIVE_CBMC", TWO_TESTED_RELEASES)
    assert cprover_pin(tc).tested
    assert cprover_pin(tc).describe().endswith("the tested release cbmc-5.96.0")


@pytest.mark.parametrize("tool", TOOLS)
def test_tested_releases_must_not_be_mixed(tool, monkeypatch):
    monkeypatch.setattr(cprover, "TESTED_INDUCTIVE_CBMC", TWO_TESTED_RELEASES)
    mixed = with_tool(measured_toolchain(), tool, path=f"/x/{tool}", version=release_line(tool, "5.96.0"))
    pin = cprover_pin(mixed)
    assert [release.problem for release in pin.tools] == [None, None, None]
    assert pin.releases_differ
    assert not pin.tested
    expected = {name: "5.96.0" if name == tool else "5.95.1" for name in TOOLS}
    assert pin.problems() == (
        "releases differ: " + ", ".join(f"{name} cbmc-{expected[name]}" for name in TOOLS),
    )
    assert pin.describe().endswith("(tested releases: cbmc-5.95.1, cbmc-5.96.0)")


def test_a_missing_tool_is_not_reported_as_a_release_mix():
    pin = cprover_pin(with_tool(measured_toolchain(), "goto-cc", path=None, version=None))
    assert not pin.releases_differ
    assert pin.problems() == ("goto-cc: not found",)


def test_a_toolchain_with_only_cbmc_is_not_tested():
    pin = cprover_pin(Toolchain(clang=None, cbmc=CBMC_PATH, clang_version=None, cbmc_version=MEASURED_CBMC_VERSION))
    assert not pin.tested
    assert [release.problem for release in pin.tools] == [None, PinProblem.NOT_FOUND, PinProblem.NOT_FOUND]
    assert pin.problems() == ("goto-cc: not found", "goto-instrument: not found")


def test_the_version_line_is_quoted_in_the_problem():
    unexpected = "5.95.1 '; rm -rf /"
    pin = cprover_pin(with_tool(measured_toolchain(), "cbmc", path=CBMC_PATH, version=unexpected))
    assert pin.problems() == (f"cbmc: version line {unexpected!r} has no single (cbmc-X.Y.Z) token",)
