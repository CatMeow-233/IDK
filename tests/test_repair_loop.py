"""Repair loop: policy enforcement, cycle detection, regression revert, write-back
and REPAIR_EXHAUSTED, using a scripted in-process provider and the real toolchain."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanc.core.context import VerifyOptions
from leanc.core.controller import verify
from leanc.core.models import ProviderInfo
from leanc.core.status import Status
from leanc.core.workspace import Workspace
from leanc.llm.base import RepairProposal
from leanc.repair.loop import is_improvement, repair_loop
from leanc.specs.approval import approve_spec
from leanc.specs.loader import load_spec

GOOD = (Path(__file__).resolve().parent.parent / "examples/safe_array_get/src/safe_array.c").read_text()
BUGGY = GOOD.replace("if (idx >= len) {", "if (idx > len) {")


class ScriptedProvider:
    """Deterministic in-process stand-in for an LLM (test double, not an LLM)."""

    def __init__(self, proposals: list[RepairProposal]):
        self.info = ProviderInfo(name="scripted-test", model=None, real_llm=False)
        self.proposals = proposals
        self.requests = []

    def propose_patch(self, req):
        self.requests.append(req)
        if req.round <= len(self.proposals):
            return self.proposals[req.round - 1]
        return RepairProposal(root_cause="", rationale="", note="no more proposals")


def files(content: str) -> dict[str, str]:
    return {"src/safe_array.c": content}


@pytest.fixture
def buggy_project(example_copy, needs_toolchain):
    root = example_copy("safe_array_get")
    (root / "src/safe_array.c").write_text(BUGGY)
    spec = load_spec(root / "spec.yaml")
    approve_spec(spec, "spec.yaml", Workspace(root), approved_by="tester")
    return root, spec


def _initial(root: Path, spec, tmp_path: Path):
    return verify(root / "spec.yaml", project_root=root, run_dir=tmp_path / "init", spec=spec, options=VerifyOptions())


def test_loop_rejects_cheats_reverts_regressions_and_accepts_real_fix(buggy_project, tmp_path):
    root, spec = buggy_project
    initial = _initial(root, spec, tmp_path)
    assert initial.status is Status.COUNTEREXAMPLE_FOUND
    cheat = BUGGY.replace("if (idx > len) {", "__CPROVER_assume(idx < len);\n    if (idx > len) {")
    harness_edit = (
        "--- a/harness/safe_array_get_harness.c\n+++ b/harness/safe_array_get_harness.c\n"
        "@@ -1,1 +1,1 @@\n-/* Trusted CBMC harness for safe_array_get (human-reviewed). */\n+/* weakened */\n"
    )
    broken = BUGGY.replace("return SAFE_ARRAY_OK;", "return SAFE_ARRAY_OK")  # compile error
    provider = ScriptedProvider(
        [
            RepairProposal(root_cause="r1", rationale="cheat", files=files(cheat)),
            RepairProposal(root_cause="r2", rationale="edit harness", patch=harness_edit),
            RepairProposal(root_cause="r3", rationale="noop", files=files(BUGGY)),
            RepairProposal(root_cause="r4", rationale="regression", files=files(broken)),
            RepairProposal(root_cause="r5", rationale="fix", files=files(GOOD)),
        ]
    )
    out = repair_loop(root / "spec.yaml", spec, root, provider, initial, run_dir=tmp_path / "rep")
    assert out.status is Status.VERIFIED_BOUNDED
    r = out.rounds
    assert [x.round for x in r] == [1, 2, 3, 4, 5]
    assert not r[0].policy.allowed and {v.rule for v in r[0].policy.violations} >= {"CPROVER_BUILTIN"}
    assert r[1].policy is not None and not r[1].policy.allowed
    assert any(v.rule in ("PATH_NOT_EDITABLE", "PATCH_INVALID") for v in r[1].policy.violations)
    assert not r[2].policy.allowed and any(v.rule == "NO_CHANGE" for v in r[2].policy.violations)
    assert r[3].status_after is Status.COMPILE_ERROR and r[3].reverted
    assert r[4].status_after is Status.VERIFIED_BOUNDED and not r[4].reverted
    assert r[4].fixed_failures, "the fixed counterexample must be listed"
    # history of earlier rounds is passed to the provider
    assert len(provider.requests[4].history) == 4
    assert provider.requests[4].failures, "counterexample reports are given to the repair agent"
    # the verified candidate is written back; protected files untouched
    assert (root / "src/safe_array.c").read_text() == GOOD
    assert out.written_back == ["src/safe_array.c"]
    assert "weakened" not in (root / "harness/safe_array_get_harness.c").read_text()
    assert (tmp_path / "rep/repair/round-5/patch.diff").read_text().startswith("--- a/src/safe_array.c")


def test_cycle_detection_and_exhaustion_leave_workspace_unchanged(buggy_project, tmp_path):
    root, spec = buggy_project
    initial = _initial(root, spec, tmp_path)
    wrong = BUGGY.replace("if (arr == NULL || out == NULL) {", "if (arr == NULL) {")
    provider = ScriptedProvider(
        [
            RepairProposal(root_cause="a", rationale="wrong", files=files(wrong)),
            RepairProposal(root_cause="a", rationale="same again", files=files(wrong)),
        ]
    )
    out = repair_loop(root / "spec.yaml", spec, root, provider, initial, run_dir=tmp_path / "rep", max_rounds=3)
    assert out.status is Status.REPAIR_EXHAUSTED
    assert len(out.rounds) == 3
    assert out.rounds[0].reverted  # more failures than before: not an improvement
    assert "cycle" in out.rounds[1].note
    assert "no more proposals" in out.rounds[2].note
    assert (root / "src/safe_array.c").read_text() == BUGGY
    assert out.written_back == []


def test_unrepairable_status_is_not_sent_to_the_provider(example_copy, needs_toolchain, tmp_path):
    root = example_copy("safe_array_get")
    spec = load_spec(root / "spec.yaml")  # not approved
    initial = verify(root / "spec.yaml", project_root=root, run_dir=tmp_path / "init", spec=spec)
    assert initial.status is Status.SPEC_UNAPPROVED
    provider = ScriptedProvider([])
    out = repair_loop(root / "spec.yaml", spec, root, provider, initial, run_dir=tmp_path / "rep")
    assert out.status is Status.SPEC_UNAPPROVED
    assert provider.requests == []


def test_is_improvement_never_accepts_inconclusive():
    from leanc.core.models import VerificationResult

    before = VerificationResult(run_id="a", status=Status.COUNTEREXAMPLE_FOUND)
    for s in (Status.UNKNOWN, Status.TIMEOUT, Status.TOOL_ERROR, Status.SPEC_UNAPPROVED):
        assert not is_improvement(before, VerificationResult(run_id="b", status=s))
    assert is_improvement(before, VerificationResult(run_id="b", status=Status.VERIFIED_BOUNDED))
    assert not is_improvement(before, VerificationResult(run_id="b", status=Status.COMPILE_ERROR))
    assert is_improvement(before, VerificationResult(run_id="b", status=Status.STATIC_ANALYSIS_WARNING))
