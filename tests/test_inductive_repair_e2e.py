from __future__ import annotations

import pytest
from inductive_iset_support import (
    CONTRACTED_LINE,
    CONTRACTS,
    CONTRACTS_FIXTURE,
    INDUCTIVE_HARNESS,
    SOURCE,
    approved,
    cli,
    contracts_text,
    iset_project,
)
from inductive_repair_support import RecordingMockProvider, write_mock_rounds

from leanc.foundation.reasons import Reason
from leanc.foundation.status import Outcome, Status, exit_code
from leanc.pipeline.controller import VERIFIED_INDUCTIVE_REASON, verify
from leanc.pipeline.repair_loop import (
    INDUCTIVE_ACCEPTED_NOTE,
    PROOF_FEEDBACK_HEAD,
    repair_loop,
)
from leanc.repair.policy import CONTRACTS_ONLY_RULE, INDUCTIVE_CODE_EDIT_RULE
from leanc.specs.loader import load_spec
from leanc.specs.model import INDUCTIVE_GATE

pytestmark = pytest.mark.usefixtures("needs_toolchain")

CLAMP = ("    int lo = 0;\n", "    if (len > (1 << 29)) {\n        return 0;\n    }\n    int lo = 0;\n")
CLAMPED_LINE = CONTRACTED_LINE + 3
WEAK_INVARIANT = ["0 <= lo", "lo <= hi"]
PROTECTED_FILES = (INDUCTIVE_HARNESS, "harness/iset_harness.c", "include/iset.h")
RANK_RETURN = "    return lower_bound(s->items, s->len, x);\n}\n\nint iset_len"
SMALL_RANK_BUG = "    if (s->len == 1 && x > s->items[0]) {\n        return 0;\n    }\n" + RANK_RETURN


def _clamped(source: str) -> str:
    clamped = source.replace(*CLAMP, 1)
    assert clamped != source
    return clamped


def _bytes(root, paths) -> dict[str, bytes]:
    return {rel: (root / rel).read_bytes() for rel in paths}


def _stage_outcomes(verification: dict) -> dict[str, str]:
    return {stage["stage"]: stage["outcome"] for stage in verification["stages"]}


def test_a_run_without_contracts_reaches_verified_inductive_with_a_mock_contracts_file(tmp_path, capsys):
    root = iset_project(tmp_path, "fixed", with_contracts=False)
    spec = approved(capsys, root)
    protected = _bytes(root, PROTECTED_FILES)
    contracts = CONTRACTS_FIXTURE.read_text(encoding="utf-8")
    mock = write_mock_rounds(tmp_path / "mock", {CONTRACTS: contracts})
    rc, payload = cli(capsys, "repair", str(spec), "--provider", "mock", "--mock-dir", str(mock))
    assert payload["initial_status"] == Status.UNKNOWN.value
    assert payload["status"] == Status.VERIFIED_INDUCTIVE.value and rc == exit_code(Status.VERIFIED_INDUCTIVE) == 0
    assert _stage_outcomes(payload["verification"])[INDUCTIVE_GATE] == Outcome.PASS.value
    (accepted,) = payload["repair_rounds"]
    assert accepted["note"] == INDUCTIVE_ACCEPTED_NOTE
    assert any(VERIFIED_INDUCTIVE_REASON in message for message in payload["messages"])
    assert (root / CONTRACTS).read_text(encoding="utf-8") == contracts
    assert _bytes(root, PROTECTED_FILES) == protected
    rc, again = cli(capsys, "verify", str(spec))
    assert again["status"] == Status.VERIFIED_INDUCTIVE.value and rc == 0


def test_a_reverted_contract_round_feeds_its_reason_codes_back(tmp_path, capsys):
    root = iset_project(tmp_path, "fixed", with_contracts=False)
    spec_path = approved(capsys, root)
    spec = load_spec(spec_path)
    initial = verify(spec_path, project_root=root, run_dir=tmp_path / "initial", spec=spec)
    assert [(code.code, code.stage) for code in initial.reason_codes] == [(Reason.LOOP_WITHOUT_CONTRACT, INDUCTIVE_GATE)]
    mock = write_mock_rounds(
        tmp_path / "mock", {CONTRACTS: contracts_text(invariant=WEAK_INVARIANT)}, {CONTRACTS: contracts_text()}
    )
    provider = RecordingMockProvider(mock)
    outcome = repair_loop(spec_path, spec, root, provider, initial, run_dir=tmp_path / "repair")
    assert outcome.status is Status.VERIFIED_INDUCTIVE
    weak, right = outcome.rounds
    assert weak.reverted and weak.status_after is Status.UNKNOWN and PROOF_FEEDBACK_HEAD in weak.note
    assert f"({INDUCTIVE_GATE}) at {SOURCE}:" in weak.note
    assert not right.reverted and right.note == INDUCTIVE_ACCEPTED_NOTE
    first, second = provider.requests
    assert first.contracts_only and second.contracts_only
    assert [code.code for code in first.reason_codes] == [Reason.LOOP_WITHOUT_CONTRACT]
    assert second.history[0]["note"] == weak.note


def test_a_clamp_that_would_reach_verified_inductive_is_rejected_in_a_proof_round(tmp_path, capsys):
    root = iset_project(tmp_path, "buggy")
    spec_path = approved(capsys, root)
    source = (root / SOURCE).read_text(encoding="utf-8")
    clamp = {SOURCE: _clamped(source), CONTRACTS: contracts_text(line=CLAMPED_LINE)}
    control = verify(spec_path, project_root=root, run_dir=tmp_path / "control", candidate=clamp)
    assert control.status is Status.VERIFIED_INDUCTIVE, control.reasons
    mock = write_mock_rounds(tmp_path / "mock", clamp)
    rc, payload = cli(
        capsys, "repair", str(spec_path), "--provider", "mock", "--mock-dir", str(mock), "--max-rounds", "1"
    )
    assert payload["initial_status"] == Status.UNKNOWN.value
    assert payload["status"] == Status.REPAIR_EXHAUSTED.value and rc == exit_code(Status.REPAIR_EXHAUSTED)
    (rejected,) = payload["repair_rounds"]
    assert rejected["policy_allowed"] is False and rejected["status_after"] is None
    assert rejected["violations"] == [CONTRACTS_ONLY_RULE] and CONTRACTS_ONLY_RULE in rejected["note"]
    assert (root / SOURCE).read_text(encoding="utf-8") == source
    assert (root / CONTRACTS).read_text(encoding="utf-8") == CONTRACTS_FIXTURE.read_text(encoding="utf-8")


def test_a_clamp_that_also_fixes_a_bounded_counterexample_is_not_written_back(tmp_path, capsys):
    root = iset_project(tmp_path, "buggy")
    buggy = (root / SOURCE).read_text(encoding="utf-8")
    assert buggy.count(RANK_RETURN) == 1
    (root / SOURCE).write_text(buggy.replace(RANK_RETURN, SMALL_RANK_BUG, 1), encoding="utf-8")
    before = _bytes(root, (SOURCE, CONTRACTS))
    spec_path = approved(capsys, root)
    clamp = {SOURCE: _clamped(buggy), CONTRACTS: contracts_text(line=CLAMPED_LINE)}
    mock = write_mock_rounds(tmp_path / "mock", clamp)
    rc, payload = cli(
        capsys, "repair", str(spec_path), "--provider", "mock", "--mock-dir", str(mock), "--max-rounds", "1"
    )
    assert payload["initial_status"] == Status.COUNTEREXAMPLE_FOUND.value
    assert payload["status"] == Status.REPAIR_EXHAUSTED.value and rc == exit_code(Status.REPAIR_EXHAUSTED)
    (blocked,) = payload["repair_rounds"]
    assert blocked["policy_allowed"] is False and blocked["reverted"] is True
    assert blocked["status_after"] == Status.VERIFIED_INDUCTIVE.value
    assert blocked["violations"] == [INDUCTIVE_CODE_EDIT_RULE]
    assert _bytes(root, (SOURCE, CONTRACTS)) == before
