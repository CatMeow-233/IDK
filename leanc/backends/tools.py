"""External tool discovery and version reporting (used by every backend and by
``leanc doctor``)."""

from __future__ import annotations

import os
import platform
import re
import shutil
import sys
from dataclasses import dataclass
from functools import lru_cache

from leanc import __version__
from leanc.core.models import EnvironmentInfo, ToolInfo
from leanc.core.runner import Limits, minimal_env, run_command
from leanc.core.sandbox import Sandbox


@dataclass(frozen=True)
class Toolchain:
    clang: str | None
    cbmc: str | None
    clang_version: str | None
    cbmc_version: str | None
    #: clang++ matching ``clang`` (used for C++ specs; see leanc.backends.cxx)
    clangxx: str | None = None
    clangxx_version: str | None = None


def _first_line(s: str) -> str:
    for line in s.splitlines():
        if line.strip():
            return line.strip()
    return ""


@lru_cache(maxsize=16)
def tool_version(path: str, flag: str = "--version") -> str | None:
    res = run_command([path, flag], cwd="/", limits=Limits(timeout_s=20), env=minimal_env())
    if res.error or res.timed_out:
        return None
    return _first_line(res.stdout or res.stderr) or None


def find_tool(name: str, env_var: str | None = None) -> str | None:
    """Locate a tool.  ``env_var`` (e.g. LEANC_CLANG) may point at an explicit
    executable; otherwise PATH (parent + standard dirs) is searched."""
    if env_var and os.environ.get(env_var):
        cand = os.environ[env_var]
        if os.path.isabs(cand) and os.access(cand, os.X_OK):
            return cand
        found = shutil.which(cand)
        if found:
            return found
        return None
    search = os.environ.get("PATH", "") + os.pathsep + minimal_env()["PATH"]
    return shutil.which(name, path=search)


def find_clang(preferred: str = "clang") -> str | None:
    if os.environ.get("LEANC_CLANG"):
        return find_tool(preferred, "LEANC_CLANG")
    for name in (preferred, "clang", "clang-19", "clang-18", "clang-17", "clang-16", "clang-15"):
        p = find_tool(name)
        if p:
            return p
    return None


def find_cbmc() -> str | None:
    return find_tool("cbmc", "LEANC_CBMC")


def clang_major(version_line: str | None) -> int | None:
    if not version_line:
        return None
    m = re.search(r"clang version (\d+)", version_line)
    return int(m.group(1)) if m else None


def toolchain(preferred_compiler: str = "clang") -> Toolchain:
    from leanc.backends.cxx import find_clangxx

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
        from leanc.backends.wp import find_framac

        fc = find_framac()
        out.append(ToolInfo(name="frama-c", path=fc.frama_c if fc else None,
                            version=tool_version(fc.real, "-version") if fc else None, available=bool(fc)))
    except Exception:  # pragma: no cover - reporting only
        pass
    try:
        from leanc.backends.lean import find_lean

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
