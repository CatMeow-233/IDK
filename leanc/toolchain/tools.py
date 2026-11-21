"""External tool discovery and version reporting (used by every backend and by
``leanc doctor``)."""

from __future__ import annotations

import os
import re
import shutil
from functools import lru_cache

from leanc.foundation.runner import Limits, minimal_env, run_command


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
