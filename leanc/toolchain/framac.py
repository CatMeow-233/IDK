"""Frama-C discovery: locate ``frama-c`` (and the ``why3`` of the same installation)."""

from __future__ import annotations

import glob
import os
import re
import shutil
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
    frama_c: str  # executable as found
    real: str  # realpath of the executable
    bin_dir: str  # directory of the real executable (opam switch bin/)
    prefix: str  # installation prefix bound into the sandbox
    why3: str | None


def _first_existing(cands: list[str]) -> str | None:
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


#: Oldest Frama-C release whose WP output (JSON report, smoke tests, terminates /
#: exits handling) this backend understands; the parsing is tested with 33.0.
MIN_FRAMAC_MAJOR = 27


def framac_version_key(version_line: str | None) -> tuple[int, int]:
    """``(major, minor)`` of a ``frama-c -version`` line such as
    ``33.0 (Arsenic)``; ``(-1, -1)`` when unknown."""
    m = re.match(r"^\s*(\d+)\.(\d+)", version_line or "")
    return (int(m.group(1)), int(m.group(2))) if m else (-1, -1)


def _candidates() -> list[str]:
    out: list[str] = []
    for d in (os.environ.get("PATH", "") + os.pathsep + DEFAULT_PATH).split(os.pathsep):
        if d:
            out.append(os.path.join(d, "frama-c"))
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
    """Locate frama-c: ``LEANC_FRAMAC`` (an executable path or name) if set;
    otherwise every ``frama-c`` on PATH (parent + standard directories) and
    ``/opt/opam/*/bin/frama-c``, preferring the newest version (a distribution
    package can be much older than the opam switch Lean C is tested with)."""
    env = os.environ.get("LEANC_FRAMAC")
    exe: str | None
    if env:
        exe = env if os.path.isabs(env) and os.access(env, os.X_OK) else shutil.which(env)
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
    # only the why3 of the same installation: its config format matches the
    # Why3 library linked into this frama-c
    why3 = _first_existing([os.path.join(bin_dir, "why3")])
    return FramaC(frama_c=exe, real=real, bin_dir=bin_dir, prefix=prefix, why3=why3)
