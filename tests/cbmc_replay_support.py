from __future__ import annotations

import json
import stat
from pathlib import Path

from cbmc_support import FIX, make_cbmc_project

from leanc.specs.model import Spec
from leanc.toolchain.context import BackendContext, make_context

VACUITY_FLAG = "-DLEANC_VACUITY_CHECK"
EDITABLE_SOURCE = "src/impl.c"
EXECUTABLE_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
DIVERGENCE_PREPROCESS_RUNS = 4
PREPROCESS_PASSTHROUGH = (
    'for a in "$@"; do last="$a"; done\n'
    'for a in "$@"; do [ "$a" = "--preprocess" ] && { echo "CBMC version 5.95.1 (cbmc-5.95.1) 64-bit"; '
    'cat "$last"; exit 0; }; done\n'
)


def fixture_messages(name: str) -> list[dict]:
    return json.loads((FIX / "json" / name).read_text())


def write_executable(path: Path, script: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + script + "\n")
    path.chmod(path.stat().st_mode | EXECUTABLE_BITS)
    return str(path)


def replaying_cbmc(directory: Path, verify: list[dict], vacuity: list[dict]) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    verify_path, vacuity_path = directory / "verify.json", directory / "vacuity.json"
    verify_path.write_text(json.dumps(verify))
    vacuity_path.write_text(json.dumps(vacuity))
    return write_executable(
        directory / "cbmc",
        PREPROCESS_PASSTHROUGH + "VAC=\n"
        f'for a in "$@"; do [ "$a" = "{VACUITY_FLAG}" ] && VAC=1; done\n'
        f'if [ -n "$VAC" ]; then cat {vacuity_path}; else cat {verify_path}; fi',
    )


def project_with_compile(
    tmp_path: Path, *, flags: list[str], defines: list[str] | None = None, source: str | None = None, **project
) -> BackendContext:
    base = make_cbmc_project(tmp_path, **project)
    if source is not None:
        (base.project_root / EDITABLE_SOURCE).write_text(source)
    data = base.spec.model_dump(mode="json")
    data["compile"]["flags"] = flags
    data["compile"]["defines"] = defines or []
    return make_context(Spec.model_validate(data), base.project_root, tmp_path / "run-compile")
