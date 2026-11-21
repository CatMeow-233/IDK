from __future__ import annotations

import os
import re

from leanc.toolchain.tools import clang_major, find_tool, tool_version

CXX_ENV = "LEANC_CLANGXX"

CXX_NOT_FOUND = (
    "C++ compiler (clang++) not found; install clang++ or point LEANC_CLANGXX at it "
    "(C++ specs are compiled, analysed, tested and integrity-checked with clang++ only)"
)

_CXX_NAME_RE = re.compile(r"^clang\+\+(-\d+(?:\.\d+)*)?$")
_C_NAME_RE = re.compile(r"^clang(-\d+(?:\.\d+)*)?$")


def _usable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _candidates_next_to(clang: str) -> list[str]:
    base = os.path.basename(clang)
    if _CXX_NAME_RE.match(base):
        return [clang]
    m = _C_NAME_RE.match(base)
    names = [f"clang++{m.group(1) or ''}"] if m else []
    names.append("clang++")
    out: list[str] = []
    for d in dict.fromkeys([os.path.dirname(clang), os.path.dirname(os.path.realpath(clang))]):
        for n in dict.fromkeys(names):
            out.append(os.path.join(d, n))
    return out


def find_clangxx(clang: str | None = None) -> str | None:
    if os.environ.get(CXX_ENV):
        return find_tool("clang++", CXX_ENV)
    cands: list[str] = []
    if clang:
        cands += [c for c in _candidates_next_to(clang) if _usable(c)]
    for name in ("clang++", "clang++-19", "clang++-18", "clang++-17", "clang++-16", "clang++-15"):
        p = find_tool(name)
        if p:
            cands.append(p)
    cands = list(dict.fromkeys(cands))
    if not cands:
        return None
    want = clang_major(tool_version(clang)) if clang else None
    if want is not None:
        for c in cands:
            if clang_major(tool_version(c)) == want:
                return c
    return cands[0]


def compiler(ctx) -> str | None:
    return ctx.tools.clangxx if ctx.spec.language.is_cpp else ctx.tools.clang


def compiler_version(ctx) -> str | None:
    return ctx.tools.clangxx_version if ctx.spec.language.is_cpp else ctx.tools.clang_version


def compiler_missing(spec, c_message: str) -> str:
    return CXX_NOT_FOUND if spec.language.is_cpp else c_message
