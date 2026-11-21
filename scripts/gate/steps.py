from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path

from scripts.gate.approvals import approval_compatibility
from scripts.gate.environment import command_env, judge_leanc_origin, leanc_origin, run_command
from scripts.gate.git import Git, remote_ref
from scripts.gate.junit import judge_junit, read_junit
from scripts.gate.model import GateState, Runner, Step, StepResult

GATE_ORDER = (
    "base",
    "commit-state",
    "worktree",
    "constraints",
    "full-suite",
    "experiments",
    "approval-compatibility",
    "worktree-unchanged",
)
OriginProbe = Callable[[str, Path, dict[str, str]], str]
ApprovalCheck = Callable[[Git, GateState, str], StepResult]


def _base_step(git: Git, state: GateState, remote: str, branch: str) -> StepResult:
    if not git.fetch(remote, branch):
        return StepResult(False, f"git fetch {remote} refs/heads/{branch} failed")
    state.base = git.resolve(remote_ref(remote, branch))
    return StepResult(bool(state.base), f"base {remote_ref(remote, branch)} = {state.base or 'unresolved'}")


def _commit_state_step(git: Git, state: GateState) -> StepResult:
    if not git.is_clean():
        return StepResult(False, "the checkout has uncommitted or untracked files: commit first")
    if state.base == state.head:
        return StepResult(False, f"HEAD {state.head} equals the base: nothing to merge, the gate would compare nothing")
    if not git.is_ancestor(state.base, state.head):
        return StepResult(False, f"base {state.base} is not an ancestor of HEAD {state.head}: merge the base first")
    return StepResult(True, f"committed; HEAD {state.head} contains base {state.base}")


def _worktree_step(git: Git, state: GateState, python: str, probe: OriginProbe) -> StepResult:
    if not git.add_worktree(state.worktree, state.head):
        return StepResult(False, f"cannot check out HEAD {state.head} into {state.worktree}")
    if git.head(state.worktree) != state.head:
        return StepResult(False, f"worktree {state.worktree} is not at {state.head}")
    probe_cwd = state.scratch / "probe"
    probe_cwd.mkdir(exist_ok=True)
    return judge_leanc_origin(probe(python, probe_cwd, command_env(state.worktree)), state.worktree)


def _command_step(name: str, argv: list[str], state: GateState, runner: Runner) -> Step:
    def run() -> StepResult:
        code = runner(argv, state.worktree, command_env(state.worktree))
        return StepResult(code == 0, f"exit {code}: {' '.join(argv)}")

    return Step(name, run)


def _full_suite_step(python: str, state: GateState, runner: Runner) -> StepResult:
    argv = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={state.junit_report}"]
    code = runner(argv, state.worktree, command_env(state.worktree))
    verdict = judge_junit(read_junit(state.junit_report))
    if code != 0:
        return StepResult(False, f"pytest exit {code}; {verdict.detail}")
    return verdict


def _worktree_unchanged_step(git: Git, state: GateState) -> StepResult:
    current = git.head(state.worktree)
    if current != state.head:
        return StepResult(False, f"worktree HEAD moved during the gate: {state.head} -> {current}")
    if not git.is_clean(state.worktree):
        return StepResult(False, "a gate step modified tracked or untracked files in the worktree")
    return StepResult(True, "worktree still at the pinned HEAD and clean")


def build_steps(
    git: Git,
    state: GateState,
    remote: str,
    branch: str,
    runner: Runner = run_command,
    probe: OriginProbe = leanc_origin,
    approval_check: ApprovalCheck = approval_compatibility,
) -> list[Step]:
    python = sys.executable
    steps = [
        Step("base", lambda: _base_step(git, state, remote, branch)),
        Step("commit-state", lambda: _commit_state_step(git, state)),
        Step("worktree", lambda: _worktree_step(git, state, python, probe)),
        _command_step("constraints", [python, "-m", "scripts.constraints"], state, runner),
        Step("full-suite", lambda: _full_suite_step(python, state, runner)),
        _command_step("experiments", [python, "scripts/run_experiments.py"], state, runner),
        Step("approval-compatibility", lambda: approval_check(git, state, python)),
        Step("worktree-unchanged", lambda: _worktree_unchanged_step(git, state)),
    ]
    return steps


def run_gate(steps: list[Step], state: GateState, clock: Callable[[], float] = time.monotonic) -> bool:
    for step in steps:
        started = clock()
        result = step.run()
        elapsed = clock() - started
        state.results.append((step.name, result, elapsed))
        print(f"{'PASS' if result.ok else 'FAIL'} {step.name} ({elapsed:.1f}s) {result.detail}", flush=True)
        if not result.ok:
            return False
    return True
