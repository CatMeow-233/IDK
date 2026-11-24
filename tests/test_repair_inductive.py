from __future__ import annotations

import pytest
import yaml
from inductive_iset_support import (
    CONTRACTED_LINE,
    CONTRACTS,
    INDUCTIVE_HARNESS,
    SOURCE,
    SPEC_FILE,
    contracts_text,
    iset_project,
)
from inductive_repair_support import (
    DECREASES_FAILURE,
    OVERFLOW_FAILURE,
    REJECTED_DETAIL,
    RESIDUAL_DETAIL,
    STEP_FAILURE,
    WP_FAILURES,
    RecordingMockProvider,
    inductive_stage,
    residual_stage,
    stage_code,
    unproved,
    verification,
    wp_stage,
    write_mock_rounds,
)

from leanc.foundation.models import StageResult
from leanc.foundation.reasons import Reason
from leanc.foundation.status import Outcome, Status
from leanc.llm.inductive_prompts import reason_code_text
from leanc.llm.prompts import build_repair_prompt
from leanc.pipeline import repair_loop as repair_loop_module
from leanc.pipeline.controller import VERIFIED_INDUCTIVE_REASON
from leanc.pipeline.repair_loop import (
    MAX_PROOF_FEEDBACK_ITEM_CHARS,
    MAX_PROOF_FEEDBACK_ITEMS,
    PROOF_FEEDBACK_HEAD,
    PROOF_GATES,
    contracts_only_round,
    is_improvement,
    proof_feedback,
    proof_repairable,
    repair_loop,
)
from leanc.repair.policy import CONTRACTS_ONLY_RULE, RULES, check_candidate
from leanc.rules.loop_contracts import LOOP_CONTRACT_RULE
from leanc.specs.loader import load_spec
from leanc.specs.model import INDUCTIVE_GATE

CLAMP = ("    int lo = 0;\n", "    if (len > (1 << 29)) {\n        return 0;\n    }\n    int lo = 0;\n")
POINTER_FAILURES = (
    unproved(
        "lower_bound.pointer_arithmetic.11",
        "pointer arithmetic: pointer outside object bounds in items + (signed long int)mid",
    ),
    unproved(
        "lower_bound.pointer_dereference.23",
        "dereference failure: pointer outside object bounds in items[(signed long int)mid]",
    ),
)
HARNESS_ASSERTION_FAILURE = unproved("main.assertion.4", "assertion 0 <= r && r <= s.len")
DISTINCT_OVERFLOWS = tuple(
    unproved(f"lower_bound.overflow.{number}", f"arithmetic overflow on signed + in lo + hi + {number}")
    for number in range(MAX_PROOF_FEEDBACK_ITEMS + 2)
)
LONG_ROOT_CAUSE = (
    "The binary search loop in lower_bound has no contract, so the inductive gate cannot run CBMC on it. "
    "The invariant bounds lo and hi by len so that the midpoint index stays inside the items array."
)
SIDE_EFFECT_CLAUSES = ("(hi = lo) >= 0", "lo++ <= hi", "lower_bound(items, len, x) >= 0")
WP_ONLY_GATES = ["compile", "static_analysis", "cbmc", "wp"]
OTHER_PROOF_GATES = tuple(gate for gate in PROOF_GATES if gate != INDUCTIVE_GATE)
LONG_STEP_FAILURE = STEP_FAILURE.model_copy(
    update={"description": "Check invariant after step for loop lower_bound.0 " + "with a long tail " * 30}
)
PLAIN_REVERTED_NOTE = f"reverted: {Status.UNKNOWN.value} is not an improvement over {Status.UNKNOWN.value}"
PLAIN_ALREADY_VERIFIED = "already verified; no repair needed"


@pytest.fixture
def project(tmp_path):
    root = iset_project(tmp_path, "fixed", with_contracts=False)
    return root, load_spec(root / SPEC_FILE)


def _sources(root) -> dict[str, str]:
    return {SOURCE: (root / SOURCE).read_text(encoding="utf-8")}


def _clamped(source: str) -> str:
    clamped = source.replace(*CLAMP, 1)
    assert clamped != source
    return clamped


def _rules(decision) -> set[str]:
    return {violation.rule for violation in decision.violations}


def _tree(root) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def _proof_stage(gate: str) -> StageResult:
    return StageResult(stage=gate, outcome=Outcome.INCONCLUSIVE, failures=list(WP_FAILURES))


class ProviderThatMustNotBeCalled(RecordingMockProvider):
    def propose_patch(self, req):
        raise AssertionError("repair of a verified result must not ask the provider")


@pytest.fixture
def wp_only_project(tmp_path):
    root = iset_project(tmp_path / "wp-only", "fixed", with_contracts=False)
    document = yaml.safe_load((root / SPEC_FILE).read_text(encoding="utf-8"))
    del document["inductive"]
    document["gates"] = WP_ONLY_GATES
    (root / SPEC_FILE).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    spec = load_spec(root / SPEC_FILE)
    assert spec.inductive is None and INDUCTIVE_GATE not in spec.gates
    return root, spec


def test_inductive_is_a_proof_gate_and_the_round_rule_is_a_policy_rule():
    assert INDUCTIVE_GATE in PROOF_GATES
    assert CONTRACTS_ONLY_RULE in RULES


def test_a_contracts_only_round_accepts_a_new_contracts_file(project):
    root, spec = project
    old = _sources(root)
    decision = check_candidate(spec, old, {**old, CONTRACTS: contracts_text()}, contracts_only=True)
    assert decision.allowed, decision.violations


def test_a_contracts_only_round_rejects_a_clamp_of_the_c_source(project):
    root, spec = project
    old = _sources(root)
    new = {SOURCE: _clamped(old[SOURCE]), CONTRACTS: contracts_text(line=CONTRACTED_LINE + 3)}
    decision = check_candidate(spec, old, new, contracts_only=True)
    assert not decision.allowed
    assert [(v.rule, v.file) for v in decision.violations] == [(CONTRACTS_ONLY_RULE, SOURCE)]


def test_the_same_clamp_passes_the_policy_outside_a_contracts_only_round(project):
    root, spec = project
    old = _sources(root)
    new = {SOURCE: _clamped(old[SOURCE]), CONTRACTS: contracts_text(line=CONTRACTED_LINE + 3)}
    assert check_candidate(spec, old, new).allowed


@pytest.mark.parametrize("contracts_only", [True, False])
@pytest.mark.parametrize("clause", SIDE_EFFECT_CLAUSES)
def test_a_side_effecting_clause_is_rejected_by_loop_contract(project, clause, contracts_only):
    root, spec = project
    old = _sources(root)
    new = {**old, CONTRACTS: contracts_text(invariant=["0 <= lo", clause])}
    decision = check_candidate(spec, old, new, contracts_only=contracts_only)
    assert _rules(decision) == {LOOP_CONTRACT_RULE}


@pytest.mark.parametrize("contracts_only", [True, False])
def test_the_inductive_harness_is_never_editable(project, contracts_only):
    root, spec = project
    old = _sources(root)
    harness = (root / INDUCTIVE_HARNESS).read_text(encoding="utf-8")
    new = {**old, INDUCTIVE_HARNESS: harness.replace("s.cap <= ISET_MAX_LEN", "s.cap <= 4")}
    decision = check_candidate(spec, old, new, contracts_only=contracts_only)
    assert "PATH_NOT_EDITABLE" in _rules(decision)
    assert all(v.file == INDUCTIVE_HARNESS for v in decision.violations)


def test_a_contracts_only_round_without_an_inductive_section_rejects_every_change(project):
    root, spec = project
    old = _sources(root)
    plain = spec.model_copy(update={"inductive": None})
    decision = check_candidate(plain, old, {SOURCE: old[SOURCE] + "\n"}, contracts_only=True)
    assert _rules(decision) == {CONTRACTS_ONLY_RULE}


@pytest.mark.parametrize(
    ("result", "contracts_only"),
    [
        (verification(residual_stage()), True),
        (verification(inductive_stage(Outcome.INCONCLUSIVE, [STEP_FAILURE])), True),
        (verification(inductive_stage(Outcome.INCONCLUSIVE, [STEP_FAILURE]), wp_stage(Outcome.INCONCLUSIVE)), True),
        (verification(inductive_stage(Outcome.PASS), wp_stage(Outcome.INCONCLUSIVE)), False),
        (verification(inductive_stage(Outcome.INCONCLUSIVE, [STEP_FAILURE]), cbmc=Outcome.FAIL), False),
        (verification(inductive_stage(Outcome.ERROR)), False),
        (verification(inductive_stage(Outcome.TIMEOUT)), False),
        (verification(inductive_stage(Outcome.PASS)), False),
    ],
)
def test_contracts_only_rounds_are_exactly_the_open_inductive_proof_rounds(result, contracts_only):
    assert contracts_only_round(result) is contracts_only


def test_an_inductive_only_unknown_is_proof_repairable():
    result = verification(residual_stage())
    assert result.status is Status.UNKNOWN and proof_repairable(result)


def test_fixing_some_unproved_loop_properties_is_proof_progress():
    before = verification(inductive_stage(Outcome.INCONCLUSIVE, [STEP_FAILURE, DECREASES_FAILURE]))
    after = verification(inductive_stage(Outcome.INCONCLUSIVE, [STEP_FAILURE]))
    assert is_improvement(before, after)


def test_emptying_the_contracts_is_not_progress():
    before = verification(inductive_stage(Outcome.INCONCLUSIVE, [STEP_FAILURE, DECREASES_FAILURE, OVERFLOW_FAILURE]))
    assert not is_improvement(before, verification(residual_stage()))


def test_a_rejected_contract_is_not_progress_over_unproved_properties():
    before = verification(inductive_stage(Outcome.INCONCLUSIVE, [STEP_FAILURE, DECREASES_FAILURE]))
    rejected = inductive_stage(Outcome.INCONCLUSIVE, extra_codes=[stage_code(Reason.CONTRACT_REJECTED, REJECTED_DETAIL)])
    assert not is_improvement(before, verification(rejected))


def test_losing_an_inductive_pass_without_failures_is_not_proof_progress():
    before = verification(inductive_stage(Outcome.PASS), wp_stage(Outcome.INCONCLUSIVE))
    after = verification(residual_stage(), wp_stage(Outcome.INCONCLUSIVE, WP_FAILURES[:1]))
    assert proof_repairable(before) and proof_repairable(after)
    assert not is_improvement(before, after)


def test_fixing_a_wp_goal_while_the_inductive_pass_holds_is_proof_progress():
    before = verification(inductive_stage(Outcome.PASS), wp_stage(Outcome.INCONCLUSIVE))
    after = verification(inductive_stage(Outcome.PASS), wp_stage(Outcome.INCONCLUSIVE, WP_FAILURES[:1]))
    assert is_improvement(before, after)


def test_a_new_counterexample_in_a_gate_that_passed_is_not_progress():
    before = verification(inductive_stage(Outcome.INCONCLUSIVE, [STEP_FAILURE, DECREASES_FAILURE]))
    after = verification(inductive_stage(Outcome.INCONCLUSIVE, [STEP_FAILURE]), cbmc=Outcome.FAIL)
    assert not is_improvement(before, after)


def test_fixing_a_counterexample_while_a_residual_loop_moved_down_is_progress():
    counterexamples = [unproved("iset_rank.overflow.1", "overflow"), unproved("iset_rank.bounds.2", "bounds")]
    shifted = RESIDUAL_DETAIL.replace(f":{CONTRACTED_LINE} ", f":{CONTRACTED_LINE + 3} ")
    moved = inductive_stage(Outcome.INCONCLUSIVE, extra_codes=[stage_code(Reason.LOOP_WITHOUT_CONTRACT, shifted)])
    before = verification(residual_stage(), cbmc=Outcome.FAIL, cbmc_failures=counterexamples)
    after = verification(moved, cbmc=Outcome.FAIL, cbmc_failures=counterexamples[:1])
    assert before.status is Status.COUNTEREXAMPLE_FOUND and is_improvement(before, after)


def test_fixing_a_counterexample_while_adding_a_residual_loop_is_not_progress():
    counterexamples = [unproved("iset_rank.overflow.1", "overflow"), unproved("iset_rank.bounds.2", "bounds")]
    before = verification(inductive_stage(Outcome.INCONCLUSIVE, [STEP_FAILURE]), cbmc=Outcome.FAIL, cbmc_failures=counterexamples)
    after = verification(residual_stage(), cbmc=Outcome.FAIL, cbmc_failures=counterexamples[:1])
    assert not is_improvement(before, after)


def test_proof_feedback_names_each_failing_class_with_its_location():
    result = verification(inductive_stage(Outcome.INCONCLUSIVE, [STEP_FAILURE, DECREASES_FAILURE]))
    feedback = proof_feedback(result)
    assert feedback.startswith(PROOF_FEEDBACK_HEAD)
    for reason in (Reason.INDUCTIVE_STEP_FAILED, Reason.SUSPECTED_NON_TERMINATION):
        assert f"{reason.value} ({INDUCTIVE_GATE}) at {SOURCE}:{CONTRACTED_LINE} in lower_bound()" in feedback


def test_proof_feedback_names_residual_loops_and_rejected_contracts():
    assert RESIDUAL_DETAIL in proof_feedback(verification(residual_stage()))
    rejected = inductive_stage(Outcome.INCONCLUSIVE, extra_codes=[stage_code(Reason.CONTRACT_REJECTED, REJECTED_DETAIL)])
    assert f"{Reason.CONTRACT_REJECTED.value} ({INDUCTIVE_GATE}): {REJECTED_DETAIL}" in proof_feedback(
        verification(rejected)
    )


def test_proof_feedback_is_bounded():
    result = verification(inductive_stage(Outcome.INCONCLUSIVE, DISTINCT_OVERFLOWS))
    assert len(result.reason_codes) > MAX_PROOF_FEEDBACK_ITEMS
    assert proof_feedback(result).count(f"({INDUCTIVE_GATE})") == MAX_PROOF_FEEDBACK_ITEMS


def test_a_passing_result_gives_no_proof_feedback():
    assert proof_feedback(verification(inductive_stage(Outcome.PASS))) == ""


def _run_rejected_rounds(tmp_path, project, monkeypatch, *rounds: dict[str, str]):
    root, spec = project

    def verify_must_not_run(*args, **kwargs):
        raise AssertionError("a candidate rejected by the repair policy must not be verified")

    monkeypatch.setattr(repair_loop_module, "verify", verify_must_not_run)
    provider = RecordingMockProvider(write_mock_rounds(tmp_path / "mock", *rounds))
    initial = verification(residual_stage())
    before = {rel: (root / rel).read_bytes() for rel in (SOURCE, INDUCTIVE_HARNESS)}
    outcome = repair_loop(root / SPEC_FILE, spec, root, provider, initial, run_dir=tmp_path / "run", max_rounds=len(rounds))
    assert outcome.status is Status.REPAIR_EXHAUSTED
    assert {rel: (root / rel).read_bytes() for rel in before} == before
    assert not (root / CONTRACTS).exists()
    return outcome, provider


def test_a_c_edit_in_a_contracts_only_round_is_rejected_before_verification(tmp_path, project, monkeypatch):
    root, _ = project
    clamp = {SOURCE: _clamped(_sources(root)[SOURCE]), CONTRACTS: contracts_text(line=CONTRACTED_LINE + 3)}
    outcome, provider = _run_rejected_rounds(tmp_path, project, monkeypatch, clamp)
    (rejected,) = outcome.rounds
    assert not rejected.policy.allowed and _rules(rejected.policy) == {CONTRACTS_ONLY_RULE}
    assert [request.contracts_only for request in provider.requests] == [True]


def test_a_side_effecting_clause_is_rejected_before_verification(tmp_path, project, monkeypatch):
    outcome, _ = _run_rejected_rounds(
        tmp_path, project, monkeypatch, {CONTRACTS: contracts_text(invariant=["0 <= lo", SIDE_EFFECT_CLAUSES[0]])}
    )
    (rejected,) = outcome.rounds
    assert _rules(rejected.policy) == {LOOP_CONTRACT_RULE}


def test_repair_never_writes_the_inductive_harness(tmp_path, project, monkeypatch):
    root, _ = project
    harness = (root / INDUCTIVE_HARNESS).read_text(encoding="utf-8")
    weakened = harness.replace("s.cap <= ISET_MAX_LEN", "s.cap <= 4")
    outcome, _ = _run_rejected_rounds(
        tmp_path, project, monkeypatch, {INDUCTIVE_HARNESS: weakened}, {INDUCTIVE_HARNESS: weakened, CONTRACTS: contracts_text()}
    )
    assert all("PATH_NOT_EDITABLE" in _rules(r.policy) for r in outcome.rounds)


def test_the_request_carries_the_reason_codes_of_the_best_verification(tmp_path, project, monkeypatch):
    _, provider = _run_rejected_rounds(tmp_path, project, monkeypatch, {SOURCE: "int broken;\n"})
    (request,) = provider.requests
    assert [(code.code, code.stage, code.detail) for code in request.reason_codes] == [
        (Reason.LOOP_WITHOUT_CONTRACT, INDUCTIVE_GATE, RESIDUAL_DETAIL)
    ]


def test_a_reverted_round_note_carries_the_proof_feedback_of_its_own_verification(tmp_path, project, monkeypatch):
    root, spec = project
    unproved_contracts = verification(inductive_stage(Outcome.INCONCLUSIVE, [STEP_FAILURE, DECREASES_FAILURE]))
    monkeypatch.setattr(repair_loop_module, "verify", lambda *args, **kwargs: unproved_contracts)
    provider = RecordingMockProvider(write_mock_rounds(tmp_path / "mock", {CONTRACTS: contracts_text()}))
    initial = verification(residual_stage())
    outcome = repair_loop(root / SPEC_FILE, spec, root, provider, initial, run_dir=tmp_path / "run", max_rounds=1)
    (reverted,) = outcome.rounds
    assert reverted.reverted and outcome.status is Status.REPAIR_EXHAUSTED
    assert reverted.note.endswith(proof_feedback(unproved_contracts)) and PROOF_FEEDBACK_HEAD in reverted.note
    assert not (root / CONTRACTS).exists()


def test_proof_feedback_puts_loop_classes_before_unproved_properties():
    failures = [*DISTINCT_OVERFLOWS, STEP_FAILURE, DECREASES_FAILURE]
    feedback = proof_feedback(verification(inductive_stage(Outcome.INCONCLUSIVE, failures)))
    assert STEP_FAILURE.property_id in feedback and DECREASES_FAILURE.property_id in feedback
    assert feedback.count("lower_bound.overflow.") == MAX_PROOF_FEEDBACK_ITEMS - 2


def test_the_next_prompt_shows_the_whole_proof_feedback_of_a_reverted_round(tmp_path, project, monkeypatch):
    root, spec = project
    six = [STEP_FAILURE, DECREASES_FAILURE, OVERFLOW_FAILURE, *POINTER_FAILURES, HARNESS_ASSERTION_FAILURE]
    unproved_contracts = verification(inductive_stage(Outcome.INCONCLUSIVE, six))
    monkeypatch.setattr(repair_loop_module, "verify", lambda *args, **kwargs: unproved_contracts)
    rounds = ({CONTRACTS: contracts_text()}, {CONTRACTS: contracts_text(decreases="hi - lo + 1")})
    provider = RecordingMockProvider(write_mock_rounds(tmp_path / "mock", *rounds, root_cause=LONG_ROOT_CAUSE))
    initial = verification(residual_stage())
    repair_loop(root / SPEC_FILE, spec, root, provider, initial, run_dir=tmp_path / "run", max_rounds=2)
    prompt = build_repair_prompt(provider.requests[1]).user
    assert proof_feedback(unproved_contracts) in prompt


@pytest.mark.parametrize("gate", OTHER_PROOF_GATES)
def test_proof_feedback_leaves_out_the_codes_of_other_proof_gates(gate):
    alone = verification(_proof_stage(gate))
    assert {code.stage for code in alone.reason_codes} == {gate} and proof_repairable(alone)
    assert proof_feedback(alone) == ""
    mixed = proof_feedback(verification(inductive_stage(Outcome.INCONCLUSIVE, [STEP_FAILURE]), _proof_stage(gate)))
    assert f"({INDUCTIVE_GATE})" in mixed and f"({gate})" not in mixed


def test_a_reverted_wp_only_round_keeps_the_plain_reverted_note(tmp_path, wp_only_project, monkeypatch):
    root, spec = wp_only_project
    unproved_goals = verification(wp_stage(Outcome.INCONCLUSIVE))
    assert any(code.stage == "wp" for code in unproved_goals.reason_codes)
    verified = []

    def verify_records(*args, **kwargs):
        verified.append(args)
        return unproved_goals

    monkeypatch.setattr(repair_loop_module, "verify", verify_records)
    provider = RecordingMockProvider(write_mock_rounds(tmp_path / "mock", {SOURCE: _sources(root)[SOURCE] + "\n"}))
    outcome = repair_loop(root / SPEC_FILE, spec, root, provider, unproved_goals, run_dir=tmp_path / "run", max_rounds=1)
    (reverted,) = outcome.rounds
    assert len(verified) == 1 and [request.contracts_only for request in provider.requests] == [False]
    assert reverted.policy.allowed and reverted.reverted
    assert reverted.note == PLAIN_REVERTED_NOTE and PROOF_FEEDBACK_HEAD not in reverted.note


def test_proof_feedback_cuts_each_item_to_its_character_limit():
    result = verification(inductive_stage(Outcome.INCONCLUSIVE, [LONG_STEP_FAILURE]))
    (code,) = result.reason_codes
    text = reason_code_text(code)
    assert len(text) > MAX_PROOF_FEEDBACK_ITEM_CHARS and "\n" not in text
    assert proof_feedback(result) == PROOF_FEEDBACK_HEAD + text[:MAX_PROOF_FEEDBACK_ITEM_CHARS]


def test_repair_of_a_verified_inductive_result_states_its_scope_and_changes_nothing(tmp_path, project):
    root, spec = project
    initial = verification(inductive_stage(Outcome.PASS))
    assert initial.status is Status.VERIFIED_INDUCTIVE
    before = _tree(root)
    provider = ProviderThatMustNotBeCalled(tmp_path / "mock")
    outcome = repair_loop(root / SPEC_FILE, spec, root, provider, initial, run_dir=tmp_path / "run", max_rounds=3)
    assert outcome.status is Status.VERIFIED_INDUCTIVE and outcome.rounds == [] and outcome.final is initial
    assert VERIFIED_INDUCTIVE_REASON in outcome.message
    assert outcome.message == f"already verified as VERIFIED_INDUCTIVE ({VERIFIED_INDUCTIVE_REASON}); no repair needed"
    assert _tree(root) == before and provider.requests == []


def test_repair_of_a_verified_bounded_result_keeps_the_plain_message(tmp_path, project):
    root, spec = project
    initial = verification()
    assert initial.status is Status.VERIFIED_BOUNDED
    before = _tree(root)
    provider = ProviderThatMustNotBeCalled(tmp_path / "mock")
    outcome = repair_loop(root / SPEC_FILE, spec, root, provider, initial, run_dir=tmp_path / "run", max_rounds=3)
    assert outcome.status is Status.VERIFIED_BOUNDED and outcome.rounds == []
    assert outcome.message == PLAIN_ALREADY_VERIFIED and _tree(root) == before
