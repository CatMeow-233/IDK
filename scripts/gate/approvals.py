from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from scripts.gate.git import EXAMPLES_DIR, Git
from scripts.gate.model import GateState, StepResult

SPEC_FILE = "spec.yaml"
GATE_APPROVER = "merge-gate"
APPROVE, STATUS = "approve", "status"
APPROVAL_PROBE = """
import sys
from pathlib import Path

code_root, action, spec = sys.argv[1:4]
sys.path.insert(0, code_root)
import leanc

if not Path(leanc.__file__).resolve().is_relative_to(Path(code_root).resolve()):
    raise SystemExit(f"leanc imported from {leanc.__file__}, not from {code_root}")
from leanc.cli import main

argv = ["spec", "approve", spec, "--by", "%s", "--yes"] if action == "approve" else ["spec", "status", spec, "--json"]
raise SystemExit(main(argv))
""" % GATE_APPROVER


def examples_unchanged_since(git: Git, base: str, head: str) -> list[str]:
    base_trees, head_trees = git.example_trees(base), git.example_trees(head)
    return sorted(name for name, tree in head_trees.items() if base_trees.get(name) == tree)


def _probe(python: str, code_root: Path, action: str, spec: Path, home: Path) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin", "LEANC_HOME": str(home), "HOME": str(home)}
    argv = [python, "-I", "-c", APPROVAL_PROBE, str(code_root), action, str(spec)]
    return subprocess.run(argv, env=env, capture_output=True, text=True, check=False)


def _still_approved(status: subprocess.CompletedProcess[str]) -> bool:
    if status.returncode != 0:
        return False
    report = json.loads(status.stdout)
    return isinstance(report, dict) and report.get("approved") is True


def _example_problem(python: str, state: GateState, name: str) -> str | None:
    project = state.scratch / "approvals" / name
    home = state.scratch / "homes" / name
    shutil.copytree(state.worktree / EXAMPLES_DIR / name, project)
    spec = project / SPEC_FILE
    approved = _probe(python, state.base_worktree, APPROVE, spec, home)
    if approved.returncode != 0:
        return f"{name}: base code could not approve ({approved.stderr.strip()[-300:]})"
    status = _probe(python, state.worktree, STATUS, spec, home)
    if not _still_approved(status):
        return f"{name}: approval made by base code is not valid under head code ({status.stdout.strip()[-300:]} {status.stderr.strip()[-300:]})"
    return None


def approval_compatibility(git: Git, state: GateState, python: str, names: list[str] | None = None) -> StepResult:
    selected = names if names is not None else examples_unchanged_since(git, state.base, state.head)
    if not selected:
        return StepResult(False, "no example is unchanged since the base: nothing to compare")
    if not git.add_worktree(state.base_worktree, state.base):
        return StepResult(False, f"cannot check out base {state.base} into {state.base_worktree}")
    try:
        problems = [p for name in selected if (p := _example_problem(python, state, name))]
    finally:
        git.remove_worktree(state.base_worktree)
    if problems:
        return StepResult(False, "existing approvals would break: " + "; ".join(problems))
    return StepResult(True, f"{len(selected)} example approval(s) made by base code stay valid under head code")
