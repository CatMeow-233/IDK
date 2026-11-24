from __future__ import annotations

import dataclasses
import json

import pytest
from inductive_iset_support import (
    CONTRACTS,
    CONTRACTS_FIXTURE,
    SOURCE,
    SPEC_FILE,
    iset_project,
)
from inductive_repair_support import (
    DECREASES_FAILURE,
    REJECTED_DETAIL,
    RESIDUAL_DETAIL,
    STEP_FAILURE,
    VACUOUS_PROBE,
    inductive_stage,
    stage_code,
    verification,
    write_mock_rounds,
)

from leanc.foundation.models import Location
from leanc.foundation.reasons import Reason
from leanc.foundation.status import Outcome
from leanc.lang.c_expr import LOOP_ENTRY, OBJECT_UPTO, OBJECT_WHOLE
from leanc.llm.base import RepairRequest
from leanc.llm.inductive_prompts import (
    CONTRACTS_ONLY_NOTE,
    INDUCTIVE_FEEDBACK_HEADING,
    MAX_FEEDBACK_CODES,
    reason_code_text,
)
from leanc.llm.mock import MockProvider
from leanc.llm.prompts import build_repair_prompt
from leanc.llm.prompts.repair import CONTRACTS_ONLY_HEADING, EDITABLE_HEADING
from leanc.llm.prompts.text import DEFAULT_LIMITS
from leanc.repair.policy import CONTRACTS_ONLY_RULE
from leanc.specs.loader import load_spec, spec_to_yaml
from leanc.specs.model import INDUCTIVE_GATE, MANDATORY_CHECKS

PROMPT_SECTION_SEPARATOR = "\n\n"
VACUITY_DETAIL = f"{VACUOUS_PROBE}: 1 of 1 occurrence(s) unreachable"
SMALL_ITEM_CHARS = 120
LONG_REJECTED_DETAIL = REJECTED_DETAIL + " and a long tail" * 200


@pytest.fixture
def spec(tmp_path):
    root = iset_project(tmp_path, "fixed", with_contracts=False)
    return load_spec(root / SPEC_FILE)


def _request(spec, *, contracts_only: bool = False, codes=()) -> RepairRequest:
    return RepairRequest(
        requirement=spec.requirement,
        spec=spec,
        spec_yaml=spec_to_yaml(spec),
        round=1,
        sources={SOURCE: "int iset_rank(const struct iset *s, int x) { return 0; }\n"},
        headers={},
        harness=None,
        tests=None,
        diagnostics=[],
        failures=[],
        history=[],
        reason_codes=list(codes),
        contracts_only=contracts_only,
    )


def _prompt(request: RepairRequest, limits=DEFAULT_LIMITS) -> str:
    return build_repair_prompt(request, limits).user


def _section(prompt: str, heading: str) -> str:
    return next(part for part in prompt.split(PROMPT_SECTION_SEPARATOR) if part.startswith(heading))


def _feedback_codes():
    stage = inductive_stage(
        Outcome.INCONCLUSIVE,
        [STEP_FAILURE, DECREASES_FAILURE],
        [
            stage_code(Reason.CONTRACT_REJECTED, REJECTED_DETAIL),
            stage_code(Reason.LOOP_WITHOUT_CONTRACT, RESIDUAL_DETAIL),
            stage_code(Reason.VACUOUS_HARNESS, VACUITY_DETAIL, Location(file=SOURCE, line=12, function="lower_bound")),
        ],
    )
    return verification(stage).reason_codes


def test_the_note_states_the_contract_file_its_schema_and_the_grammar(spec):
    prompt = _prompt(_request(spec))
    for text in (CONTRACTS, spec.inductive.harness, *MANDATORY_CHECKS, LOOP_ENTRY, OBJECT_UPTO, OBJECT_WHOLE):
        assert text in prompt, text
    for key in json.loads(CONTRACTS_FIXTURE.read_text(encoding="utf-8"))["loops"][0]:
        assert f'"{key}"' in prompt, key
    assert "iset_rank(), iset_contains()" in prompt
    assert "proposals only" in prompt and "not a counterexample" in prompt


def test_the_feedback_lists_failing_classes_rejected_contracts_residual_loops_and_vacuous_probes(spec):
    feedback = _section(_prompt(_request(spec, codes=_feedback_codes())), INDUCTIVE_FEEDBACK_HEADING)
    where = f"at {SOURCE}:11 in lower_bound()"
    assert f"{Reason.INDUCTIVE_STEP_FAILED.value} ({INDUCTIVE_GATE}) {where}: {STEP_FAILURE.property_id}" in feedback
    assert f"{Reason.SUSPECTED_NON_TERMINATION.value} ({INDUCTIVE_GATE}) {where}" in feedback
    assert f"{Reason.CONTRACT_REJECTED.value} ({INDUCTIVE_GATE}): {REJECTED_DETAIL}" in feedback
    assert f"{Reason.LOOP_WITHOUT_CONTRACT.value} ({INDUCTIVE_GATE}): {RESIDUAL_DETAIL}" in feedback
    assert f"{Reason.VACUOUS_HARNESS.value} ({INDUCTIVE_GATE}) at {SOURCE}:12 in lower_bound(): {VACUITY_DETAIL}" in feedback


def test_the_feedback_shows_only_inductive_codes_and_is_bounded(spec):
    codes = _feedback_codes()
    many = [codes[0]] * (MAX_FEEDBACK_CODES + 5) + [codes[0].model_copy(update={"stage": "cbmc"})]
    feedback = _section(_prompt(_request(spec, codes=many)), INDUCTIVE_FEEDBACK_HEADING)
    assert feedback.count(f"({INDUCTIVE_GATE})") == MAX_FEEDBACK_CODES
    assert "(cbmc)" not in feedback


def test_a_contracts_only_round_lists_only_the_contracts_file_as_changeable(spec):
    prompt = _prompt(_request(spec, contracts_only=True))
    assert _section(prompt, CONTRACTS_ONLY_HEADING) == f"{CONTRACTS_ONLY_HEADING}\n- {CONTRACTS}"
    assert EDITABLE_HEADING not in prompt
    assert CONTRACTS_ONLY_NOTE.format(contracts=CONTRACTS) in prompt and CONTRACTS_ONLY_RULE in prompt


def test_an_open_round_lists_every_editable_file(spec):
    prompt = _prompt(_request(spec))
    assert _section(prompt, EDITABLE_HEADING) == f"{EDITABLE_HEADING}\n- {SOURCE}\n- {CONTRACTS}"
    assert CONTRACTS_ONLY_HEADING not in prompt and CONTRACTS_ONLY_RULE not in prompt


def test_a_spec_without_the_inductive_gate_gets_no_inductive_text(spec):
    plain = spec.model_copy(update={"gates": [gate for gate in spec.gates if gate != INDUCTIVE_GATE]})
    prompt = _prompt(_request(plain, codes=_feedback_codes()))
    assert INDUCTIVE_FEEDBACK_HEADING not in prompt
    assert "Inductive loop contracts" not in prompt


def test_the_mock_provider_replays_a_contracts_file(tmp_path, spec):
    contracts = CONTRACTS_FIXTURE.read_text(encoding="utf-8")
    provider = MockProvider(write_mock_rounds(tmp_path / "mock", {CONTRACTS: contracts}))
    proposal = provider.propose_patch(_request(spec))
    assert proposal.files == {CONTRACTS: contracts} and proposal.patch is None


@pytest.mark.parametrize("limits", [DEFAULT_LIMITS, dataclasses.replace(DEFAULT_LIMITS, max_item_chars=SMALL_ITEM_CHARS)])
def test_the_feedback_cuts_each_item_to_the_item_limit(spec, limits):
    stage = inductive_stage(Outcome.INCONCLUSIVE, extra_codes=[stage_code(Reason.CONTRACT_REJECTED, LONG_REJECTED_DETAIL)])
    (code,) = verification(stage).reason_codes
    text = reason_code_text(code)
    assert len(text) > limits.max_item_chars and "\n" not in text
    feedback = _section(_prompt(_request(spec, codes=[code]), limits), INDUCTIVE_FEEDBACK_HEADING)
    assert feedback == f"{INDUCTIVE_FEEDBACK_HEADING}\n- {text[: limits.max_item_chars]}"
