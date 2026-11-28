from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from scripts.gate.approvals import (
    APPROVE,
    _probe,
    approval_compatibility,
    examples_unchanged_since,
)
from scripts.gate.environment import command_env, judge_leanc_origin
from scripts.gate.git import Git
from scripts.gate.junit import JunitCounts, judge_junit, read_junit
from scripts.gate.model import GateState, StepResult
from scripts.gate.steps import GATE_ORDER, build_steps, run_gate
from scripts.merge_gate import main

REPO = Path(__file__).resolve().parent.parent
HEAD = "a" * 40
BASE = "b" * 40
OTHER = "c" * 40
PASSING_JUNIT = '<testsuites><testsuite name="pytest" tests="3" failures="0" errors="0" skipped="0"/></testsuites>'


class FakeGit:
    def __init__(self, *, clean=True, ancestor=True, fetch_ok=True, base=BASE, worktree_ok=True, worktree_head=HEAD,
                 final_head=None, final_clean=True, trees=None):
        self.clean = clean
        self.ancestor = ancestor
        self.fetch_ok = fetch_ok
        self.base = base
        self.worktree_ok = worktree_ok
        self.worktree_heads = [worktree_head, final_head or worktree_head]
        self.final_clean = final_clean
        self.trees = trees or {}

    def head(self, cwd=None) -> str:
        return self.worktree_heads.pop(0) if len(self.worktree_heads) > 1 else self.worktree_heads[0]

    def fetch(self, remote, branch) -> bool:
        return self.fetch_ok

    def resolve(self, ref) -> str:
        return self.base

    def is_clean(self, cwd=None) -> bool:
        return self.final_clean if cwd is not None else self.clean

    def is_ancestor(self, ancestor, descendant) -> bool:
        return self.ancestor

    def example_trees(self, rev) -> dict[str, str]:
        return self.trees.get(rev, {})

    def add_worktree(self, path, sha) -> bool:
        if self.worktree_ok:
            Path(path).mkdir(parents=True, exist_ok=True)
        return self.worktree_ok

    def remove_worktree(self, path) -> None:
        return None


class Recorder:
    def __init__(self, failing: str | None = None, junit: str = PASSING_JUNIT):
        self.calls: list[list[str]] = []
        self.failing = failing
        self.junit = junit

    def __call__(self, argv, cwd, env) -> int:
        self.calls.append(argv)
        junit = next((a.split("=", 1)[1] for a in argv if a.startswith("--junitxml=")), None)
        if junit:
            Path(junit).write_text(self.junit)
        return 1 if self.failing and self.failing in " ".join(argv) else 0


def _gate(tmp_path, git, runner, approvals_ok=True, origin=None):
    state = GateState(head=HEAD, scratch=tmp_path)
    approval_calls = []

    def approval_check(g, s, python):
        approval_calls.append(s.base)
        return StepResult(approvals_ok, "stub")

    def probe(python, cwd, env):
        return origin if origin is not None else str(state.worktree / "leanc" / "__init__.py")

    steps = build_steps(git, state, "origin", "main", runner=runner, probe=probe, approval_check=approval_check)
    return run_gate(steps, state, clock=lambda: 0.0), state, approval_calls


def _last_step(state) -> str:
    return state.results[-1][0]


def test_all_steps_run_in_order_and_pass(tmp_path):
    runner = Recorder()
    passed, state, approval_calls = _gate(tmp_path, FakeGit(), runner)
    assert passed and tuple(name for name, _, _ in state.results) == GATE_ORDER
    assert [call[1:3] for call in runner.calls] == [["-m", "scripts.constraints"], ["-m", "pytest"], ["scripts/run_experiments.py"]]
    assert approval_calls == [BASE]


@pytest.mark.parametrize(
    "failing, stops_at, commands_run",
    [("scripts.constraints", "constraints", 1), ("pytest", "full-suite", 2), ("run_experiments", "experiments", 3)],
)
def test_gate_stops_at_the_first_failing_command(tmp_path, failing, stops_at, commands_run):
    runner = Recorder(failing)
    passed, state, approval_calls = _gate(tmp_path, FakeGit(), runner)
    assert not passed and _last_step(state) == stops_at
    assert len(runner.calls) == commands_run and approval_calls == []


@pytest.mark.parametrize(
    "git, stops_at",
    [
        (FakeGit(fetch_ok=False), "base"),
        (FakeGit(base=""), "base"),
        (FakeGit(clean=False), "commit-state"),
        (FakeGit(base=HEAD), "commit-state"),
        (FakeGit(ancestor=False), "commit-state"),
        (FakeGit(worktree_ok=False), "worktree"),
        (FakeGit(worktree_head=OTHER), "worktree"),
        (FakeGit(final_head=OTHER), "worktree-unchanged"),
        (FakeGit(final_clean=False), "worktree-unchanged"),
    ],
)
def test_gate_refuses_unpinned_or_dirty_state(tmp_path, git, stops_at):
    runner = Recorder()
    passed, state, _ = _gate(tmp_path, git, runner)
    assert not passed and _last_step(state) == stops_at
    if stops_at in ("base", "commit-state", "worktree"):
        assert runner.calls == []


def test_gate_refuses_a_leanc_outside_the_worktree(tmp_path):
    passed, state, _ = _gate(tmp_path, FakeGit(), Recorder(), origin="/home/user/IDK/leanc/__init__.py")
    assert not passed and _last_step(state) == "worktree"


@pytest.mark.parametrize(
    "junit",
    [
        '<testsuites><testsuite tests="3" failures="0" errors="0" skipped="1"/></testsuites>',
        '<testsuites><testsuite tests="3" failures="1" errors="0" skipped="0"/></testsuites>',
        '<testsuites><testsuite tests="0" failures="0" errors="0" skipped="0"/></testsuites>',
        "<not-xml",
    ],
)
def test_full_suite_requires_every_test_to_run_and_pass(tmp_path, junit):
    passed, state, _ = _gate(tmp_path, FakeGit(), Recorder(junit=junit))
    assert not passed and _last_step(state) == "full-suite"


def test_approval_failure_fails_the_gate(tmp_path):
    passed, state, _ = _gate(tmp_path, FakeGit(), Recorder(), approvals_ok=False)
    assert not passed and _last_step(state) == "approval-compatibility"


def test_read_and_judge_junit(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(PASSING_JUNIT)
    assert read_junit(report) == JunitCounts(tests=3, failures=0, errors=0, skipped=0)
    assert judge_junit(read_junit(report)).ok
    assert not judge_junit(read_junit(tmp_path / "missing.xml")).ok


def test_command_env_strips_python_and_pytest_overrides(tmp_path):
    env = command_env(tmp_path, {"PATH": "/usr/bin", "PYTEST_ADDOPTS": "--collect-only", "PYTHONPATH": "/elsewhere",
                                 "PYTHONSTARTUP": "x.py", "LEANC_CLANG": "clang"})
    assert env == {"PATH": "/usr/bin", "LEANC_CLANG": "clang", "PYTHONPATH": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}


def test_judge_leanc_origin(tmp_path):
    assert judge_leanc_origin(str(tmp_path / "leanc" / "__init__.py"), tmp_path).ok
    assert not judge_leanc_origin("/usr/lib/leanc/__init__.py", tmp_path).ok
    assert not judge_leanc_origin("", tmp_path).ok


def test_only_unchanged_example_directories_are_compared():
    trees = {BASE: {"x": "1", "y": "2"}, HEAD: {"x": "1", "y": "9", "z": "3"}}
    assert examples_unchanged_since(FakeGit(trees=trees), BASE, HEAD) == ["x"]


def test_no_unchanged_example_fails_closed(tmp_path):
    result = approval_compatibility(FakeGit(), GateState(head=HEAD, scratch=tmp_path, base=BASE), sys.executable)
    assert not result.ok and "nothing to compare" in result.detail


def test_approval_made_by_base_stays_valid_under_own_head(tmp_path, needs_git):
    git = Git(REPO)
    head = git.head()
    state = GateState(head=head, scratch=tmp_path, base=head)
    assert git.add_worktree(state.worktree, head)
    try:
        result = approval_compatibility(git, state, sys.executable, names=["safe_array_get"])
    finally:
        git.remove_worktree(state.worktree)
    assert result.ok, result.detail


def test_approval_probe_refuses_a_shadowed_leanc(tmp_path):
    spec = REPO / "examples" / "safe_array_get" / "spec.yaml"
    with tempfile.TemporaryDirectory() as empty:
        proc = _probe(sys.executable, Path(empty), APPROVE, spec, tmp_path / "home")
    assert proc.returncode != 0


def test_gate_has_no_skip_option():
    with pytest.raises(SystemExit):
        main(["--skip", "full-suite"])


def test_experiments_fail_when_no_example_is_selected(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "run_experiments.py"), "--examples", str(tmp_path), "--out", str(tmp_path / "out")],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 1 and "nothing was verified" in proc.stderr
