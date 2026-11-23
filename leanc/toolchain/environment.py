from __future__ import annotations

import platform
import shutil
import sys
from dataclasses import dataclass

from leanc import __version__
from leanc.foundation.models import EnvironmentInfo, ToolInfo
from leanc.foundation.sandbox import Sandbox
from leanc.toolchain.cxx import find_clangxx
from leanc.toolchain.framac import find_framac
from leanc.toolchain.lean import find_lean
from leanc.toolchain.tools import (
    GOTO_CC,
    GOTO_INSTRUMENT,
    find_cbmc,
    find_clang,
    find_goto_cc,
    find_goto_instrument,
    tool_version,
)


@dataclass(frozen=True)
class Toolchain:
    clang: str | None
    cbmc: str | None
    clang_version: str | None
    cbmc_version: str | None
    clangxx: str | None = None
    clangxx_version: str | None = None
    goto_cc: str | None = None
    goto_cc_version: str | None = None
    goto_instrument: str | None = None
    goto_instrument_version: str | None = None


def _version_of(path: str | None) -> str | None:
    return tool_version(path) if path else None


def toolchain(preferred_compiler: str = "clang") -> Toolchain:
    clang = find_clang(preferred_compiler)
    cbmc = find_cbmc()
    clangxx = find_clangxx(clang)
    goto_cc = find_goto_cc()
    goto_instrument = find_goto_instrument()
    return Toolchain(
        clang=clang,
        cbmc=cbmc,
        clang_version=_version_of(clang),
        cbmc_version=_version_of(cbmc),
        clangxx=clangxx,
        clangxx_version=_version_of(clangxx),
        goto_cc=goto_cc,
        goto_cc_version=_version_of(goto_cc),
        goto_instrument=goto_instrument,
        goto_instrument_version=_version_of(goto_instrument),
    )


def _framac_info() -> ToolInfo:
    fc = find_framac()
    if fc is None:
        return ToolInfo(name="frama-c", path=None, version=None, available=False)
    return ToolInfo(name="frama-c", path=fc.frama_c, version=tool_version(fc.real, "-version"), available=True)


def _lean_info() -> ToolInfo:
    lean = find_lean()
    if lean is None:
        return ToolInfo(name="lean", path=None, version=None, available=False)
    return ToolInfo(name="lean", path=lean.lean, version=lean.version, available=bool(lean.leanchecker))


def _v2_tool_infos() -> list[ToolInfo]:
    return [_framac_info(), _lean_info()]


def _cprover_tool_infos(tc: Toolchain) -> list[ToolInfo]:
    return [
        ToolInfo(name=GOTO_CC, path=tc.goto_cc, version=tc.goto_cc_version, available=bool(tc.goto_cc)),
        ToolInfo(
            name=GOTO_INSTRUMENT,
            path=tc.goto_instrument,
            version=tc.goto_instrument_version,
            available=bool(tc.goto_instrument),
        ),
    ]


def environment_info(sandbox: Sandbox, tc: Toolchain | None = None) -> EnvironmentInfo:
    tc = tc or toolchain()
    tools = [
        ToolInfo(name="clang", path=tc.clang, version=tc.clang_version, available=bool(tc.clang)),
        ToolInfo(name="cbmc", path=tc.cbmc, version=tc.cbmc_version, available=bool(tc.cbmc)),
        *_cprover_tool_infos(tc),
        ToolInfo(name="clang++", path=tc.clangxx, version=tc.clangxx_version, available=bool(tc.clangxx)),
        *_v2_tool_infos(),
    ]
    bw = shutil.which("bwrap")
    tools.append(
        ToolInfo(
            name="bwrap",
            path=bw,
            version=tool_version(bw) if bw else None,
            available=sandbox.kind == "bwrap",
            note=None if sandbox.kind == "bwrap" else sandbox.note,
        )
    )
    return EnvironmentInfo(
        leanc_version=__version__,
        python=sys.version.split()[0],
        platform=f"{platform.system()} {platform.release()} {platform.machine()}",
        tools=tools,
        sandbox=sandbox.kind,
        sandbox_note=sandbox.note,
    )
