from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass

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
    chk = os.path.join(prefix, "bin", "leanchecker")
    return LeanToolchain(
        lean=real,
        prefix=prefix,
        version=m.group(1) if m else None,
        version_line=line,
        leanchecker=chk if os.path.isfile(chk) and os.access(chk, os.X_OK) else None,
    )


def _version_key(path: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", path)]


def find_lean() -> LeanToolchain | None:
    env = os.environ.get("LEANC_LEAN")
    if env:
        cands = [env, os.path.join(env, "bin", "lean"), os.path.join(env, "lean")] if os.path.isdir(env) else [env]
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
    for c in sorted(glob.glob("/opt/lean/*/bin/lean"), key=_version_key, reverse=True):
        tc = _toolchain_from(c)
        if tc:
            return tc
    return None
