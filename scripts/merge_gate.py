from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

from scripts.gate.git import Git
from scripts.gate.model import GateState
from scripts.gate.steps import build_steps, run_gate

REPO = Path(__file__).resolve().parent.parent
DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"
PASS_BANNER = "MERGE GATE PASS"
FAIL_BANNER = "MERGE GATE FAIL"
SCRATCH_PREFIX = "leanc-merge-gate-"
DESCRIPTION = "fix -> test -> merge gate: every step runs on a fresh worktree of the pinned HEAD and must pass"


def _banner(passed: bool, state: GateState) -> str:
    if passed:
        return f"{PASS_BANNER} head={state.head} base={state.base}"
    failed = next(name for name, result, _ in state.results if not result.ok)
    return f"{FAIL_BANNER} at {failed} head={state.head} base={state.base or 'unresolved'}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.merge_gate", description=DESCRIPTION)
    parser.add_argument("--remote", default=DEFAULT_REMOTE)
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help="base branch the PR merges into")
    args = parser.parse_args(argv)
    git = Git(REPO)
    state = GateState(head=git.head(), scratch=Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX)))
    try:
        passed = run_gate(build_steps(git, state, args.remote, args.branch), state)
    finally:
        if state.worktree.exists():
            git.remove_worktree(state.worktree)
        shutil.rmtree(state.scratch, ignore_errors=True)
    print(_banner(passed, state))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
