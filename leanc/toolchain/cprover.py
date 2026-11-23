from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from leanc.toolchain.environment import Toolchain

__all__ = [
    "CBMC_RELEASE_TOKEN_RE",
    "GOTO_CC_GCC_PERSONALITY_PREFIX",
    "TESTED_INDUCTIVE_CBMC",
    "CproverPin",
    "CproverTool",
    "PinProblem",
    "ToolRelease",
    "cbmc_release",
    "cprover_pin",
    "pinned_releases_text",
]

TESTED_INDUCTIVE_CBMC: tuple[str, ...] = ("5.95.1",)
CBMC_RELEASE_TOKEN_RE = re.compile(r"\(cbmc-([^()\s]+)\)")
GOTO_CC_GCC_PERSONALITY_PREFIX = "gcc (goto-cc "
RELEASE_TOKEN_PREFIX = "cbmc-"


class CproverTool(str, Enum):
    CBMC = "cbmc"
    GOTO_CC = "goto-cc"
    GOTO_INSTRUMENT = "goto-instrument"


class PinProblem(str, Enum):
    NOT_FOUND = "not_found"
    NO_VERSION_LINE = "no_version_line"
    NO_RELEASE_TOKEN = "no_release_token"
    NOT_GCC_PERSONALITY = "not_gcc_personality"
    UNTESTED_RELEASE = "untested_release"


def cbmc_release(version_line: str | None) -> str | None:
    tokens = CBMC_RELEASE_TOKEN_RE.findall(version_line or "")
    return tokens[0] if len(tokens) == 1 else None


def pinned_releases_text() -> str:
    return ", ".join(RELEASE_TOKEN_PREFIX + release for release in TESTED_INDUCTIVE_CBMC)


def _release_text(release: str | None) -> str:
    return RELEASE_TOKEN_PREFIX + release if release else "no release token"


@dataclass(frozen=True)
class ToolRelease:
    tool: CproverTool
    path: str | None
    version_line: str | None

    @property
    def release(self) -> str | None:
        return cbmc_release(self.version_line)

    def _runs_as_gcc(self) -> bool:
        return (self.version_line or "").startswith(GOTO_CC_GCC_PERSONALITY_PREFIX)

    @property
    def problem(self) -> PinProblem | None:
        if not self.path:
            return PinProblem.NOT_FOUND
        if not self.version_line:
            return PinProblem.NO_VERSION_LINE
        if self.release is None:
            return PinProblem.NO_RELEASE_TOKEN
        if self.tool is CproverTool.GOTO_CC and not self._runs_as_gcc():
            return PinProblem.NOT_GCC_PERSONALITY
        if self.release not in TESTED_INDUCTIVE_CBMC:
            return PinProblem.UNTESTED_RELEASE
        return None

    def status(self) -> str:
        problem = self.problem
        if problem is PinProblem.NOT_FOUND:
            return "not found"
        if problem is PinProblem.NO_VERSION_LINE:
            return "the --version probe printed nothing"
        if problem is PinProblem.NO_RELEASE_TOKEN:
            return f"version line {self.version_line!r} has no single (cbmc-X.Y.Z) token"
        if problem is PinProblem.NOT_GCC_PERSONALITY:
            return f"version line {self.version_line!r} does not start with {GOTO_CC_GCC_PERSONALITY_PREFIX!r}"
        if problem is PinProblem.UNTESTED_RELEASE:
            return f"release {_release_text(self.release)} is not tested for the inductive gate"
        return f"release {_release_text(self.release)} is tested for the inductive gate"

    def describe(self) -> str:
        return f"{self.tool.value}: {self.status()}"


@dataclass(frozen=True)
class CproverPin:
    cbmc: ToolRelease
    goto_cc: ToolRelease
    goto_instrument: ToolRelease

    @property
    def tools(self) -> tuple[ToolRelease, ToolRelease, ToolRelease]:
        return (self.cbmc, self.goto_cc, self.goto_instrument)

    @property
    def releases_differ(self) -> bool:
        releases = {tool.release for tool in self.tools}
        return None not in releases and len(releases) > 1

    def problems(self) -> tuple[str, ...]:
        found = [tool.describe() for tool in self.tools if tool.problem is not None]
        if self.releases_differ:
            found.append(
                "releases differ: "
                + ", ".join(f"{tool.tool.value} {_release_text(tool.release)}" for tool in self.tools)
            )
        return tuple(found)

    @property
    def tested(self) -> bool:
        return not self.problems()

    def describe(self) -> str:
        problems = self.problems()
        if problems:
            return "; ".join(problems) + f" (tested releases: {pinned_releases_text()})"
        return f"cbmc, goto-cc and goto-instrument report the tested release {_release_text(self.cbmc.release)}"


def cprover_pin(tc: Toolchain) -> CproverPin:
    return CproverPin(
        cbmc=ToolRelease(CproverTool.CBMC, tc.cbmc, tc.cbmc_version),
        goto_cc=ToolRelease(CproverTool.GOTO_CC, tc.goto_cc, tc.goto_cc_version),
        goto_instrument=ToolRelease(CproverTool.GOTO_INSTRUMENT, tc.goto_instrument, tc.goto_instrument_version),
    )
