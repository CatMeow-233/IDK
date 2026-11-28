from __future__ import annotations

import glob
import os
import re
import shutil
import sys
from dataclasses import dataclass

from leanc.foundation.runner import DEFAULT_PATH
from leanc.toolchain.tools import tool_version

__all__ = ["INSTALL_HINT", "MIN_FRAMAC_MAJOR", "FramaC", "find_framac", "framac_version_key"]

INSTALL_HINT = (
    "install Frama-C with the WP plug-in and an SMT prover, e.g. `opam install frama-c alt-ergo` "
    "(then `why3 config detect`), and put frama-c on PATH or set LEANC_FRAMAC=/path/to/frama-c"
)


@dataclass(frozen=True)
class FramaC:
    frama_c: str
    real: str
    bin_dir: str
    prefix: str
    why3: str | None


def _first_existing(cands: list[str]) -> str | None:
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


MIN_FRAMAC_MAJOR = 27
UNKNOWN_VERSION_KEY = (-1, -1)


def framac_version_key(version_line: str | None) -> tuple[int, int]:
    m = re.match(r"^\s*(\d+)\.(\d+)", version_line or "")
    return (int(m.group(1)), int(m.group(2))) if m else UNKNOWN_VERSION_KEY


def _exe_names(base: str) -> list[str]:
    return [f"{base}.exe", base] if sys.platform == "win32" else [base]


def _why3_of_same_installation(bin_dir: str) -> str | None:
    return _first_existing([os.path.join(bin_dir, name) for name in _exe_names("why3")])


def _candidates() -> list[str]:
    out: list[str] = []
    for d in (os.environ.get("PATH", "") + os.pathsep + DEFAULT_PATH).split(os.pathsep):
        if d:
            for name in _exe_names("frama-c"):
                out.append(os.path.join(d, name))
    if sys.platform != "win32":
        out += sorted(glob.glob("/opt/opam/*/bin/frama-c"))
    seen: set[str] = set()
    uniq: list[str] = []
    for c in out:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            real = os.path.realpath(c)
            if real not in seen:
                seen.add(real)
                uniq.append(c)
    return uniq


def find_framac() -> FramaC | None:
    env = os.environ.get("LEANC_FRAMAC")
    exe: str | None
    if env:
        exe = env if os.path.isabs(env) and os.path.isfile(env) and os.access(env, os.X_OK) else shutil.which(env)
        if not exe:
            return None
    else:
        cands = _candidates()
        if not cands:
            return None
        exe = max(cands, key=lambda c: framac_version_key(tool_version(os.path.realpath(c), "-version")))
    real = os.path.realpath(exe)
    bin_dir = os.path.dirname(real)
    prefix = os.path.dirname(bin_dir) if os.path.basename(bin_dir) == "bin" else bin_dir
    return FramaC(frama_c=exe, real=real, bin_dir=bin_dir, prefix=prefix, why3=_why3_of_same_installation(bin_dir))
