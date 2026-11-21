from __future__ import annotations

import argparse
import fnmatch
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"
SPEC_FILE_PATTERNS = ("examples/*/spec*.yaml", "examples/*/spec*.json", "leanc/data/templates/*/spec.yaml")
PASS_BANNER = "MERGE GATE PASS"
FAIL_BANNER = "MERGE GATE FAIL"
DESCRIPTION = "fix -> test -> merge gate: every step must pass on the pinned HEAD before a PR is merged"
SPEC_HASH_PROBE = """
import json
import sys
from pathlib import Path

code_root, spec_root, *specs = sys.argv[1:]
sys.path.insert(0, code_root)
import leanc

if not Path(leanc.__file__).resolve().is_relative_to(Path(code_root).resolve()):
    raise SystemExit(f"leanc imported from {leanc.__file__}, not from {code_root}")
from leanc.foundation.errors import LeanCError
from leanc.specs.loader import load_spec, spec_hash

hashes = {}
for rel in specs:
    try:
        hashes[rel] = spec_hash(load_spec(Path(spec_root) / rel))
    except LeanCError as exc:
        hashes[rel] = f"rejected: {exc}"
print(json.dumps(hashes))
"""


@dataclass(frozen=True)
class StepResult:
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class Step:
    name: str
    run: Callable[[], StepResult]


@dataclass
class GateState:
    head: str
    base: str = ""
    results: list[tuple[str, StepResult, float]] = field(default_factory=list)


class Git:
    def __init__(self, root: Path = REPO) -> None:
        self.root = root

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, check=False)

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def fetch(self, remote: str, branch: str) -> bool:
        return self._git("fetch", "-q", remote, branch).returncode == 0

    def resolve(self, ref: str) -> str:
        proc = self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def is_clean(self) -> bool:
        proc = self._git("status", "--porcelain", "--untracked-files=normal")
        return proc.returncode == 0 and not proc.stdout.strip()

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self._git("merge-base", "--is-ancestor", ancestor, descendant).returncode == 0

    def blobs(self, ref: str) -> dict[str, str]:
        proc = self._git("ls-tree", "-r", ref)
        entries = (line.split(None, 3) for line in proc.stdout.splitlines())
        return {path: blob for _, kind, blob, path in entries if kind == "blob"}

    def add_worktree(self, path: Path, sha: str) -> bool:
        return self._git("worktree", "add", "--detach", str(path), sha).returncode == 0

    def remove_worktree(self, path: Path) -> None:
        self._git("worktree", "remove", "--force", str(path))
        self._git("worktree", "prune")


def run_command(argv: list[str]) -> int:
    return subprocess.run(argv, cwd=REPO, check=False).returncode


def command_step(name: str, argv: list[str], runner: Callable[[list[str]], int]) -> Step:
    def run() -> StepResult:
        code = runner(argv)
        return StepResult(code == 0, f"exit {code}: {' '.join(argv)}")

    return Step(name, run)


def spec_files_unchanged_since(git: Git, base: str, head: str) -> list[str]:
    base_blobs, head_blobs = git.blobs(base), git.blobs(head)
    return sorted(
        path
        for path, blob in head_blobs.items()
        if base_blobs.get(path) == blob and any(fnmatch.fnmatch(path, p) for p in SPEC_FILE_PATTERNS)
    )


def hash_mismatches(base: dict[str, str], head: dict[str, str]) -> list[str]:
    return [
        f"{path}: base {base.get(path)!r} != head {head.get(path)!r}"
        for path in sorted(set(base) | set(head))
        if base.get(path) != head.get(path)
    ]


def _probe_hashes(code_root: Path, specs: list[str]) -> dict[str, str] | str:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", SPEC_HASH_PROBE, str(code_root), str(REPO), *specs],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return f"hash probe failed in {code_root}: {proc.stderr.strip()[-500:]}"
    return json.loads(proc.stdout)


def spec_hash_stability(git: Git, state: GateState) -> StepResult:
    specs = spec_files_unchanged_since(git, state.base, state.head)
    if not specs:
        return StepResult(False, "no spec files shared with the base: nothing to compare")
    scratch = Path(tempfile.mkdtemp(prefix="leanc-merge-gate-"))
    worktree = scratch / "base"
    try:
        if not git.add_worktree(worktree, state.base):
            return StepResult(False, f"cannot check out base {state.base} into {worktree}")
        base_hashes = _probe_hashes(worktree, specs)
        head_hashes = _probe_hashes(REPO, specs)
    finally:
        git.remove_worktree(worktree)
        shutil.rmtree(scratch, ignore_errors=True)
    for probe in (base_hashes, head_hashes):
        if isinstance(probe, str):
            return StepResult(False, probe)
    mismatches = hash_mismatches(base_hashes, head_hashes)
    if mismatches:
        return StepResult(False, "spec_hash changed for unchanged specs (existing approvals break): " + "; ".join(mismatches))
    return StepResult(True, f"{len(specs)} unchanged spec(s) hash identically under base and head code")


def build_steps(
    git: Git,
    state: GateState,
    remote: str,
    branch: str,
    runner: Callable[[list[str]], int] = run_command,
    hash_check: Callable[[Git, GateState], StepResult] = spec_hash_stability,
) -> list[Step]:
    python = sys.executable

    def base() -> StepResult:
        if not git.fetch(remote, branch):
            return StepResult(False, f"git fetch {remote} {branch} failed")
        state.base = git.resolve(f"{remote}/{branch}")
        return StepResult(bool(state.base), f"base {remote}/{branch} = {state.base or 'unresolved'}")

    def tree() -> StepResult:
        if not git.is_clean():
            return StepResult(False, "working tree has uncommitted or untracked files: commit first")
        if not git.is_ancestor(state.base, state.head):
            return StepResult(False, f"base {state.base} is not an ancestor of HEAD {state.head}: merge or rebase first")
        return StepResult(True, f"clean tree; HEAD {state.head} contains base")

    def head_unchanged() -> StepResult:
        current = git.head()
        if current != state.head:
            return StepResult(False, f"HEAD moved during the gate: {state.head} -> {current}")
        if not git.is_clean():
            return StepResult(False, "working tree changed during the gate")
        return StepResult(True, "HEAD unchanged and tree clean")

    return [
        Step("base", base),
        Step("tree", tree),
        command_step("constraints", [python, "scripts/constraints.py"], runner),
        command_step("full-suite", [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"], runner),
        command_step("experiments", [python, "scripts/run_experiments.py"], runner),
        Step("spec-hash-stability", lambda: hash_check(git, state)),
        Step("head-unchanged", head_unchanged),
    ]


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--remote", default=DEFAULT_REMOTE)
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help="base branch the PR merges into")
    args = parser.parse_args(argv)
    git = Git()
    state = GateState(head=git.head())
    passed = run_gate(build_steps(git, state, args.remote, args.branch), state)
    failed = next((name for name, result, _ in state.results if not result.ok), None)
    print(f"{PASS_BANNER} {state.head}" if passed else f"{FAIL_BANNER} at {failed} (HEAD {state.head})")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
