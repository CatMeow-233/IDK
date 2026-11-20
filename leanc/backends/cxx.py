"""C++ toolchain helpers (V2 C++ support).

A spec with ``language: {name: cpp, standard: c++17|c++20}`` is built,
analysed, tested and integrity-checked with ``clang++`` only - never with the
C driver - so every gate judges the same C++ program (same front end, same
``-std``, same standard library headers).

Discovery of ``clang++`` (:func:`find_clangxx`):

1. ``$LEANC_CLANGXX`` names it explicitly (an unusable value yields ``None``:
   an explicit choice never silently falls back to something else);
2. otherwise the C++ driver *next to* the ``clang`` that is used for C, with
   the same version suffix (``clang-18`` -> ``clang++-18``, ``clang`` ->
   ``clang++``), first in the directory of the path as given, then in the
   directory it resolves to (e.g. ``/usr/lib/llvm-18/bin``);
3. otherwise ``clang++`` / ``clang++-N`` on ``PATH``.

Candidates whose ``--version`` reports the same major version as ``clang``
are preferred.  The path is returned *unresolved*: clang selects C++ mode
from the name it is invoked as, and ``realpath(/usr/bin/clang++)`` is the
plain ``clang`` binary.
"""

from __future__ import annotations

import os
import re

from leanc.backends.tools import clang_major, find_tool, tool_version

CXX_ENV = "LEANC_CLANGXX"

#: Summary used by every gate when a C++ spec finds no clang++.
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
    """Locate the clang++ driver matching ``clang`` (see module docstring)."""
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
    """The compiler driver for ``ctx.spec``: clang++ for C++, clang for C."""
    return ctx.tools.clangxx if ctx.spec.language.is_cpp else ctx.tools.clang


def compiler_version(ctx) -> str | None:
    return ctx.tools.clangxx_version if ctx.spec.language.is_cpp else ctx.tools.clang_version


def compiler_missing(spec, c_message: str) -> str:
    """The "compiler not found" summary for ``spec`` (``c_message`` for C)."""
    return CXX_NOT_FOUND if spec.language.is_cpp else c_message
