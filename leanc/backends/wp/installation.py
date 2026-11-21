"""The Frama-C installation seen by the WP gate: tool PATH, provers, Why3 configuration and sandbox mounts."""

from __future__ import annotations

import os
import re
import shutil

from leanc.foundation.runner import DEFAULT_PATH
from leanc.toolchain.framac import FramaC

ALWAYS_MOUNTED_DIR = "/usr"
REPLAYING_OR_INTERACTIVE_PROVERS = frozenset(
    {"none", "script", "tip", "coq", "isabelle", "proofgeneral", "why3ide", "native", "native:alt-ergo"}
)

_WHY3_SECTION_START_RE = re.compile(r"(?m)^\[")
_WHY3_PROVER_SECTIONS = ("partial_prover]", "prover]")
_WHY3_PROVER_FIELD_RE = re.compile(r'(?m)^(name|version|path|alternative)\s*=\s*"([^"]*)"')


def is_automatic_prover(prover: str) -> bool:
    family = prover.split(":", 1)[0].lower()
    return family not in REPLAYING_OR_INTERACTIVE_PROVERS and prover.lower() not in REPLAYING_OR_INTERACTIVE_PROVERS


def tool_path(fc: FramaC) -> str:
    return f"{fc.bin_dir}{os.pathsep}{DEFAULT_PATH}"


def prover_executable(fc: FramaC, prover: str) -> str | None:
    return shutil.which(prover.split(":", 1)[0].lower(), path=tool_path(fc))


def framac_share_dir(fc: FramaC) -> str:
    nested = os.path.join(fc.prefix, "share", "frama-c", "share")
    return nested if os.path.lexists(nested) else os.path.join(fc.prefix, "share", "frama-c")


def _is_mounted_by_default(directory: str) -> bool:
    return directory == ALWAYS_MOUNTED_DIR or directory.startswith(ALWAYS_MOUNTED_DIR + "/")


def _is_inside_any(directory: str, mounts: list[str]) -> bool:
    return any(directory == mount or directory.startswith(mount + "/") for mount in mounts)


def extra_read_only_mounts(fc: FramaC, provers: list[str]) -> list[str]:
    """Directories outside ``/usr`` that Frama-C, Why3 and the provers need inside the sandbox."""
    mounts: list[str] = []
    why3_dir = os.path.dirname(os.path.realpath(fc.why3)) if fc.why3 else None
    for directory in [fc.prefix, why3_dir]:
        if directory and not _is_mounted_by_default(directory) and directory not in mounts:
            mounts.append(directory)
    for prover in provers:
        executable = prover_executable(fc, prover)
        if executable:
            directory = os.path.dirname(os.path.realpath(executable))
            if not _is_mounted_by_default(directory) and not _is_inside_any(directory, mounts):
                mounts.append(directory)
    return mounts


def why3_config_provers(conf_text: str) -> list[dict]:
    """The ``[partial_prover]`` / ``[prover]`` sections of a Why3 configuration."""
    provers: list[dict] = []
    for section in _WHY3_SECTION_START_RE.split(conf_text):
        if not section.startswith(_WHY3_PROVER_SECTIONS):
            continue
        entry = dict(_WHY3_PROVER_FIELD_RE.findall(section))
        if entry.get("name"):
            provers.append(entry)
    return provers
