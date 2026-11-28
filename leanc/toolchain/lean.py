from __future__ import annotations

import glob
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from leanc.toolchain.tools import find_tool, tool_version

__all__ = ["INSTALL_HINT", "LeanToolchain", "find_lean"]

INSTALL_HINT = (
    "Lean 4 not found: install a Lean 4 toolchain (release archive from "
    "https://github.com/leanprover/lean4/releases unpacked e.g. to /opt/lean/<version>, or elan) and put its "
    "bin/lean on PATH or set LEANC_LEAN=/path/to/toolchain/bin/lean (no Mathlib needed)"
)

CORE_LIBRARY_OLEAN = "Init.olean"
_VERSION_RE = re.compile(r"version (\d+\.\d+\.\d+[\w.\-]*)")


@dataclass(frozen=True)
class LeanToolchain:
    lean: str
    prefix: str
    version: str | None
    version_line: str | None
    leanchecker: str | None

    @property
    def lib_dir(self) -> str:
        return os.path.join(self.prefix, "lib", "lean")


def _has_core_library(prefix: str) -> bool:
    return os.path.isfile(os.path.join(prefix, "lib", "lean", CORE_LIBRARY_OLEAN))


def _exe_names(base: str) -> list[str]:
    return [f"{base}.exe", base] if sys.platform == "win32" else [base]


def _toolchain_from(path: str) -> LeanToolchain | None:
    real = os.path.realpath(path)
    if not (os.path.isfile(real) and os.access(real, os.X_OK)):
        return None
    prefix = os.path.dirname(os.path.dirname(real))
    if not _has_core_library(prefix):
        return None
    line = tool_version(real)
    if not line or not line.startswith("Lean"):
        return None
    m = _VERSION_RE.search(line)
    checker = next(
        (
            cand
            for name in _exe_names("leanchecker")
            if os.path.isfile(cand := os.path.join(prefix, "bin", name)) and os.access(cand, os.X_OK)
        ),
        None,
    )
    return LeanToolchain(
        lean=real,
        prefix=prefix,
        version=m.group(1) if m else None,
        version_line=line,
        leanchecker=checker,
    )


def _version_key(path: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", path)]


def _elan_lean_candidates() -> list[str]:
    elan_bin = Path.home() / ".elan" / "bin"
    return [str(elan_bin / name) for name in _exe_names("lean")]


def find_lean() -> LeanToolchain | None:
    env = os.environ.get("LEANC_LEAN")
    if env:
        if os.path.isdir(env):
            cands = [
                env,
                *(os.path.join(env, "bin", name) for name in _exe_names("lean")),
                *(os.path.join(env, name) for name in _exe_names("lean")),
            ]
        else:
            cands = [env]
        if not os.path.isabs(env) and not os.path.isdir(env):
            found = find_tool(env)
            cands = [found] if found else []
        for c in cands:
            if os.path.isfile(c):
                tc = _toolchain_from(c)
                if tc:
                    return tc
        return None
    p = find_tool("lean")
    if p:
        tc = _toolchain_from(p)
        if tc:
            return tc
    extra = sorted(glob.glob("/opt/lean/*/bin/lean"), key=_version_key, reverse=True) if sys.platform != "win32" else []
    for c in [*extra, *_elan_lean_candidates()]:
        tc = _toolchain_from(c)
        if tc:
            return tc
    return None
