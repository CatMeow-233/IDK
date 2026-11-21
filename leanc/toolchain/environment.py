"""The resolved toolchain of a verification run and the environment report (tool paths and versions)."""

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
from leanc.toolchain.tools import find_cbmc, find_clang, tool_version


@dataclass(frozen=True)
class Toolchain:
    clang: str | None
    cbmc: str | None
    clang_version: str | None
    cbmc_version: str | None
    #: clang++ matching ``clang`` (used for C++ specs; see leanc.toolchain.cxx)
    clangxx: str | None = None
    clangxx_version: str | None = None


def toolchain(preferred_compiler: str = "clang") -> Toolchain:
    clang = find_clang(preferred_compiler)
    cbmc = find_cbmc()
    clangxx = find_clangxx(clang)
    return Toolchain(
        clang=clang,
        cbmc=cbmc,
        clang_version=tool_version(clang) if clang else None,
        cbmc_version=tool_version(cbmc) if cbmc else None,
        clangxx=clangxx,
        clangxx_version=tool_version(clangxx) if clangxx else None,
    )


def _v2_tool_infos() -> list[ToolInfo]:
    """Optional V2 tools (Frama-C/WP, Lean 4), reported for reproducibility."""
    out: list[ToolInfo] = []
    try:
        fc = find_framac()
        out.append(ToolInfo(name="frama-c", path=fc.frama_c if fc else None,
                            version=tool_version(fc.real, "-version") if fc else None, available=bool(fc)))
    except Exception:  # pragma: no cover - reporting only
        pass
    try:
        lt = find_lean()
        out.append(ToolInfo(name="lean", path=lt.lean if lt else None, version=lt.version if lt else None,
                            available=bool(lt and lt.leanchecker)))
    except Exception:  # pragma: no cover - reporting only
        pass
    return out


def environment_info(sandbox: Sandbox, tc: Toolchain | None = None) -> EnvironmentInfo:
    tc = tc or toolchain()
    tools = [
        ToolInfo(name="clang", path=tc.clang, version=tc.clang_version, available=bool(tc.clang)),
        ToolInfo(name="cbmc", path=tc.cbmc, version=tc.cbmc_version, available=bool(tc.cbmc)),
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
