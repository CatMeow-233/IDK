from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from scripts.merge_gate import (
    Git,
    GateState,
    StepResult,
    _probe_hashes,
    build_steps,
    hash_mismatches,
    main,
    run_gate,
    spec_files_unchanged_since,
    spec_hash_stability,
)

REPO = Path(__file__).resolve().parent.parent
GATE_ORDER = ["base", "tree", "constraints", "full-suite", "experiments", "spec-hash-stability", "head-unchanged"]
HEAD = "a" * 40
BASE = "b" * 40


class FakeGit:
    def __init__(self, *, heads=(HEAD,), clean=(True,), ancestor=True, fetch_ok=True, base=BASE, blobs=None):
        self.heads = list(heads)
        self.clean = list(clean)
        self.ancestor = ancestor
        self.fetch_ok = fetch_ok
        self.base = base
        self.blob_map = blobs or {}

    def head(self) -> str:
        return self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]

    def fetch(self, remote: str, branch: str) -> bool:
        return self.fetch_ok

    def resolve(self, ref: str) -> str:
        return self.base

    def is_clean(self) -> bool:
        return self.clean.pop(0) if len(self.clean) > 1 else self.clean[0]

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.ancestor

    def blobs(self, ref: str) -> dict[str, str]:
        return self.blob_map.get(ref, {})


class Recorder:
    def __init__(self, failing: str | None = None):
        self.calls: list[list[str]] = []
        self.failing = failing

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(argv)
        return 1 if self.failing and self.failing in " ".join(argv) else 0


def _gate(git, runner, hash_ok=True):
    state = GateState(head=HEAD)
    hash_calls = []

    def hash_check(g, s):
        hash_calls.append(s.base)
        return StepResult(hash_ok, "stub")

    steps = build_steps(git, state, "origin", "main", runner=runner, hash_check=hash_check)
    return run_gate(steps, state, clock=lambda: 0.0), state, hash_calls


def test_all_steps_run_in_order_and_pass():
    runner = Recorder()
    passed, state, hash_calls = _gate(FakeGit(), runner)
    assert passed and [name for name, _, _ in state.results] == GATE_ORDER
    assert [call[1:] for call in runner.calls] == [
        ["scripts/constraints.py"],
        ["-m", "pytest", "-q", "-p", "no:cacheprovider"],
        ["scripts/run_experiments.py"],
    ]
    assert hash_calls == [BASE] and state.base == BASE


@pytest.mark.parametrize(
    "failing, stops_at, commands_run",
    [("constraints.py", "constraints", 1), ("pytest", "full-suite", 2), ("run_experiments", "experiments", 3)],
)
def test_gate_stops_at_the_first_failing_command(failing, stops_at, commands_run):
    runner = Recorder(failing)
    passed, state, hash_calls = _gate(FakeGit(), runner)
    assert not passed and state.results[-1][0] == stops_at
    assert len(runner.calls) == commands_run and hash_calls == []


@pytest.mark.parametrize(
    "git, stops_at",
    [
        (FakeGit(fetch_ok=False), "base"),
        (FakeGit(base=""), "base"),
        (FakeGit(clean=(False,)), "tree"),
        (FakeGit(ancestor=False), "tree"),
        (FakeGit(heads=("c" * 40,)), "head-unchanged"),
        (FakeGit(clean=(True, False)), "head-unchanged"),
    ],
)
def test_gate_refuses_unpinned_or_dirty_state(git, stops_at):
    runner = Recorder()
    passed, state, _ = _gate(git, runner)
    assert not passed and state.results[-1][0] == stops_at
    if stops_at in ("base", "tree"):
        assert runner.calls == []


def test_spec_hash_failure_fails_the_gate():
    passed, state, _ = _gate(FakeGit(), Recorder(), hash_ok=False)
    assert not passed and state.results[-1][0] == "spec-hash-stability"


def test_hash_mismatches():
    assert hash_mismatches({"a": "1"}, {"a": "1"}) == []
    assert len(hash_mismatches({"a": "1"}, {"a": "2"})) == 1
    assert len(hash_mismatches({"a": "1"}, {})) == 1


def test_only_unchanged_spec_files_are_compared():
    blobs = {
        BASE: {"examples/x/spec.yaml": "1", "examples/y/spec.yaml": "2", "src/a.c": "3"},
        HEAD: {"examples/x/spec.yaml": "1", "examples/y/spec.yaml": "9", "src/a.c": "3", "examples/z/spec.yaml": "4"},
    }
    assert spec_files_unchanged_since(FakeGit(blobs=blobs), BASE, HEAD) == ["examples/x/spec.yaml"]


def test_no_shared_spec_files_fails_closed():
    result = spec_hash_stability(FakeGit(), GateState(head=HEAD, base=BASE))
    assert not result.ok and "nothing to compare" in result.detail


def test_spec_hash_stability_against_own_head_passes():
    git = Git(REPO)
    head = git.head()
    result = spec_hash_stability(git, GateState(head=head, base=head))
    assert result.ok, result.detail


def test_hash_probe_refuses_a_shadowed_leanc():
    with tempfile.TemporaryDirectory() as empty:
        assert isinstance(_probe_hashes(Path(empty), ["examples/safe_array_get/spec.yaml"]), str)


def test_gate_has_no_skip_option():
    with pytest.raises(SystemExit):
        main(["--skip", "full-suite"])
