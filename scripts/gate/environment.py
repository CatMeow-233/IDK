from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from scripts.gate.model import StepResult

STRIPPED_ENV_PREFIXES = ("PYTEST_", "PYTHON")
LEANC_ORIGIN_PROBE = "import leanc; print(leanc.__file__)"


def command_env(code_root: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if not k.startswith(STRIPPED_ENV_PREFIXES)}
    env["PYTHONPATH"] = str(code_root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_command(argv: list[str], cwd: Path, env: Mapping[str, str]) -> int:
    return subprocess.run(argv, cwd=cwd, env=dict(env), check=False).returncode


def leanc_origin(python: str, cwd: Path, env: Mapping[str, str]) -> str:
    proc = subprocess.run([python, "-c", LEANC_ORIGIN_PROBE], cwd=cwd, env=dict(env), capture_output=True, text=True, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def judge_leanc_origin(origin: str, code_root: Path) -> StepResult:
    if not origin:
        return StepResult(False, "cannot import leanc with the gate environment")
    if not Path(origin).resolve().is_relative_to(code_root.resolve()):
        return StepResult(False, f"leanc resolves to {origin}, not to the gate worktree {code_root}")
    return StepResult(True, f"leanc resolves into the worktree ({origin})")
