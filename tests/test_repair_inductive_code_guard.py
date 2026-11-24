from __future__ import annotations

import pytest
from inductive_iset_support import CONTRACTED_LINE, CONTRACTS, SOURCE, SPEC_FILE, contracts_text, iset_project
from inductive_repair_support import (
    STEP_FAILURE,
    RecordingMockProvider,
    inductive_stage,
    unproved,
    verification,
    write_mock_rounds,
)

from leanc.foundation.models import StageResult
from leanc.foundation.status import Outcome, Status
from leanc.pipeline import repair_loop as repair_loop_module
from leanc.pipeline.inductive_code_guard import BLOCKED_CANDIDATE_DIR, InductiveCodeGuard, inductive_open
from leanc.pipeline.repair_loop import repair_loop
from leanc.repair.policy import INDUCTIVE_CODE_EDIT_RULE, RULES
from leanc.specs.loader import load_spec

CLAMP = ("    int lo = 0;\n", "    if (len > (1 << 29)) {\n        return 0;\n    }\n    int lo = 0;\n")
COUNTEREXAMPLES = (unproved("iset_rank.overflow.1", "overflow"), unproved("iset_rank.bounds.2", "bounds"))
MAX_ROUNDS = 3


@pytest.fixture
def project(tmp_path):
    root = iset_project(tmp_path, "buggy")
    return root, load_spec(root / SPEC_FILE)


def _clamped(source: str) -> str:
    clamped = source.replace(*CLAMP, 1)
    assert clamped != source
    return clamped


def _rules(round_) -> set[str]:
    return {violation.rule for violation in round_.policy.violations}


def _workspace(root) -> dict[str, bytes]:
    return {rel: (root / rel).read_bytes() for rel in (SOURCE, CONTRACTS)}


def mixed(failures=COUNTEREXAMPLES):
    return verification(inductive_stage(Outcome.INCONCLUSIVE, [STEP_FAILURE]), cbmc=Outcome.FAIL, cbmc_failures=list(failures))


def verified_inductive():
    result = verification(inductive_stage(Outcome.PASS))
    assert result.status is Status.VERIFIED_INDUCTIVE
    return result


def _run(tmp_path, project, monkeypatch, initial, results, *rounds, max_rounds=MAX_ROUNDS):
    root, spec = project
    pending = list(results)
    monkeypatch.setattr(repair_loop_module, "verify", lambda *args, **kwargs: pending.pop(0))
    provider = RecordingMockProvider(write_mock_rounds(tmp_path / "mock", *rounds))
    outcome = repair_loop(root / SPEC_FILE, spec, root, provider, initial, run_dir=tmp_path / "run", max_rounds=max_rounds)
    return outcome, provider


def test_the_rule_is_a_documented_policy_rule():
    assert INDUCTIVE_CODE_EDIT_RULE == "INDUCTIVE_CODE_EDIT" and INDUCTIVE_CODE_EDIT_RULE in RULES


def test_a_clamp_in_a_mixed_round_that_reaches_verified_inductive_is_blocked_and_not_written_back(tmp_path, project, monkeypatch):
    root, _ = project
    before = _workspace(root)
    clamp = {SOURCE: _clamped((root / SOURCE).read_text(encoding="utf-8")), CONTRACTS: contracts_text(line=CONTRACTED_LINE + 3)}
    initial = mixed()
    assert initial.status is Status.COUNTEREXAMPLE_FOUND
    outcome, provider = _run(tmp_path, project, monkeypatch, initial, [verified_inductive()], clamp)
    assert [request.contracts_only for request in provider.requests] == [False]
    assert outcome.status is Status.REPAIR_EXHAUSTED and outcome.final is initial and outcome.written_back == []
    (blocked,) = outcome.rounds
    assert blocked.reverted and not blocked.policy.allowed and _rules(blocked) == {INDUCTIVE_CODE_EDIT_RULE}
    assert [violation.file for violation in blocked.policy.violations] == [SOURCE]
    assert blocked.status_after is Status.VERIFIED_INDUCTIVE and INDUCTIVE_CODE_EDIT_RULE in blocked.note
    assert INDUCTIVE_CODE_EDIT_RULE in outcome.message and "a human must review" in outcome.message
    saved = tmp_path / "run" / "repair" / "round-1" / BLOCKED_CANDIDATE_DIR / SOURCE
    assert saved.read_text(encoding="utf-8") == clamp[SOURCE]
    assert _workspace(root) == before


def test_a_code_edit_accepted_earlier_blocks_a_later_inductive_pass(tmp_path, project, monkeypatch):
    root, _ = project
    before = _workspace(root)
    first = {SOURCE: (root / SOURCE).read_text(encoding="utf-8") + "\n"}
    second = {CONTRACTS: contracts_text(decreases="hi - lo + 1")}
    results = [mixed(COUNTEREXAMPLES[:1]), verified_inductive()]
    outcome, _ = _run(tmp_path, project, monkeypatch, mixed(), results, first, second)
    accepted, blocked = outcome.rounds
    assert not accepted.reverted and accepted.policy.allowed
    assert blocked.reverted and _rules(blocked) == {INDUCTIVE_CODE_EDIT_RULE}
    assert [violation.file for violation in blocked.policy.violations] == [SOURCE]
    assert outcome.status is Status.REPAIR_EXHAUSTED and _workspace(root) == before


def test_a_mixed_round_code_edit_that_leaves_the_inductive_gate_open_is_still_progress(tmp_path, project, monkeypatch):
    root, _ = project
    edit = {SOURCE: (root / SOURCE).read_text(encoding="utf-8") + "\n"}
    outcome, _ = _run(tmp_path, project, monkeypatch, mixed(), [mixed(COUNTEREXAMPLES[:1])], edit, max_rounds=1)
    (accepted,) = outcome.rounds
    assert not accepted.reverted and accepted.policy.allowed and accepted.policy.violations == []


def test_a_contracts_only_repair_of_untouched_code_still_reaches_verified_inductive(tmp_path, project, monkeypatch):
    root, _ = project
    initial = verification(inductive_stage(Outcome.INCONCLUSIVE, [STEP_FAILURE]))
    contracts = {CONTRACTS: contracts_text(decreases="hi - lo + 1")}
    outcome, provider = _run(tmp_path, project, monkeypatch, initial, [verified_inductive()], contracts)
    assert [request.contracts_only for request in provider.requests] == [True]
    assert outcome.status is Status.VERIFIED_INDUCTIVE and outcome.written_back == [CONTRACTS]
    assert (root / CONTRACTS).read_text(encoding="utf-8") == contracts[CONTRACTS]


def test_a_code_fix_while_the_inductive_gate_already_passes_is_accepted(tmp_path, project, monkeypatch):
    root, _ = project
    initial = verification(inductive_stage(Outcome.PASS), cbmc=Outcome.FAIL)
    assert initial.status is Status.COUNTEREXAMPLE_FOUND
    fixed = (root / SOURCE).read_text(encoding="utf-8") + "\n"
    outcome, _ = _run(tmp_path, project, monkeypatch, initial, [verified_inductive()], {SOURCE: fixed})
    assert outcome.status is Status.VERIFIED_INDUCTIVE and outcome.written_back == [SOURCE]
    assert (root / SOURCE).read_text(encoding="utf-8") == fixed


@pytest.mark.parametrize(
    "outcome, required, armed",
    [
        (Outcome.PASS, True, False),
        (Outcome.INCONCLUSIVE, True, True),
        (Outcome.SKIPPED, True, True),
        (Outcome.TIMEOUT, True, True),
        (Outcome.ERROR, True, True),
        (Outcome.INCONCLUSIVE, False, False),
    ],
)
def test_the_guard_is_armed_exactly_when_a_required_inductive_gate_is_open(project, outcome, required, armed):
    _, spec = project
    stage = StageResult(stage="inductive", outcome=outcome, required=required)
    assert inductive_open(spec, verification(stage)) is armed
    assert InductiveCodeGuard.start(spec, verification(stage), {}).armed is armed


def test_a_missing_inductive_stage_arms_the_guard_when_the_spec_has_the_gate(project):
    _, spec = project
    assert inductive_open(spec, verification()) is True
