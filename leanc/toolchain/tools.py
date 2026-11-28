from __future__ import annotations

import os
import re
import shutil
import sys
from functools import lru_cache

from leanc.foundation.runner import Limits, minimal_env, run_command

DARWIN_LLVM_DIRS = ("/opt/homebrew/opt/llvm/bin", "/usr/local/opt/llvm/bin")
WINDOWS_EXTRA_TOOL_DIRS = (
    r"C:\Program Files\LLVM\bin",
    r"C:\Program Files\cbmc\bin",
    r"C:\msys64\ucrt64\bin",
    r"C:\msys64\mingw64\bin",
    r"C:\mingw64\bin",
)
CLANG_CANDIDATE_NAMES = (
    "clang",
    "clang-21",
    "clang-20",
    "clang-19",
    "clang-18",
    "clang-17",
    "clang-16",
    "clang-15",
)


def _first_line(s: str) -> str:
    for line in s.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _version_probe_cwd() -> str:
    return os.path.abspath(os.sep) if os.name == "nt" else "/"


def _platform_extra_dirs() -> list[str]:
    if sys.platform == "darwin":
        return [d for d in DARWIN_LLVM_DIRS if os.path.isdir(d)]
    if sys.platform == "win32":
        program_files = os.environ.get("ProgramFiles")
        dirs = list(WINDOWS_EXTRA_TOOL_DIRS)
        if program_files:
            dirs = [os.path.join(program_files, "LLVM", "bin"), os.path.join(program_files, "cbmc", "bin"), *dirs]
        return [d for d in dict.fromkeys(dirs) if os.path.isdir(d)]
    return []


@lru_cache(maxsize=16)
def tool_version(path: str, flag: str = "--version") -> str | None:
    res = run_command([path, flag], cwd=_version_probe_cwd(), limits=Limits(timeout_s=20), env=minimal_env())
    if res.error or res.timed_out:
        return None
    return _first_line(res.stdout or res.stderr) or None


def find_tool(name: str, env_var: str | None = None) -> str | None:
    if env_var and os.environ.get(env_var):
        cand = os.environ[env_var]
        if os.path.isabs(cand) and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
        found = shutil.which(cand)
        if found:
            return found
        return None
    parts = [os.environ.get("PATH", ""), minimal_env()["PATH"], *_platform_extra_dirs()]
    search = os.pathsep.join(part for part in parts if part)
    return shutil.which(name, path=search)


def find_clang(preferred: str = "clang") -> str | None:
    if os.environ.get("LEANC_CLANG"):
        return find_tool(preferred, "LEANC_CLANG")
    for name in dict.fromkeys((preferred, *CLANG_CANDIDATE_NAMES)):
        p = find_tool(name)
        if p:
            return p
    return None


def find_cbmc() -> str | None:
    return find_tool("cbmc", "LEANC_CBMC")


GOTO_CC = "goto-cc"
GOTO_CC_ENV = "LEANC_GOTO_CC"
GOTO_INSTRUMENT = "goto-instrument"
GOTO_INSTRUMENT_ENV = "LEANC_GOTO_INSTRUMENT"


def find_goto_cc() -> str | None:
    return find_tool(GOTO_CC, GOTO_CC_ENV)


def find_goto_instrument() -> str | None:
    return find_tool(GOTO_INSTRUMENT, GOTO_INSTRUMENT_ENV)


def clang_major(version_line: str | None) -> int | None:
    if not version_line:
        return None
    m = re.search(r"clang version (\d+)", version_line)
    return int(m.group(1)) if m else None
