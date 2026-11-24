from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from inductive_e2e_support import (
    claims_anything,
    flat,
    inductive_record,
    mentions_claim,
    no_claim_statement,
    reason_pairs,
    report_texts,
    stages_by_name,
)
from inductive_iset_support import FIXTURES, INSERT_CLAIM, SPEC_FILE, approved, cli, iset_project

from leanc.foundation.reasons import Reason
from leanc.foundation.status import Outcome, Status, exit_code, is_success
from leanc.specs.loader import load_spec
from leanc.specs.model import INDUCTIVE_GATE

pytestmark = [pytest.mark.slow, pytest.mark.usefixtures("needs_toolchain")]

EXAMPLE = "inductive_sponge"
SPONGE_SOURCE = "src/sponge.c"
BUGGY_SPONGE = FIXTURES / "acceptance" / "sponge" / "buggy" / SPONGE_SOURCE
ABSORB_LOOP_LINE = 35
SPONGE_GATES = ["spec", "compile", "integrity", "static_analysis", "sanitizer", "cbmc", INDUCTIVE_GATE]
SPONGE_SCOPE = (
    "claimed functions: sponge_absorb(); functions that are not claimed have only the bounded CBMC result",
    "contracted loops proved by induction (invariant at entry and after one step, assigns, decreases): "
    "src/sponge.c:9 in sponge_mix(), src/sponge.c:35 in sponge_absorb()",
    "harness assumptions of harness/sponge_inductive.c (executions outside them are not covered): "
    "harness/sponge_inductive.c:11: s != ((void *)0); harness/sponge_inductive.c:12: s->fill < 144; "
    "harness/sponge_inductive.c:14: len <= ((size_t)1 << 48); harness/sponge_inductive.c:16: data != ((void *)0)",
    "checked property classes: the mandatory CBMC check classes only (bounds, pointer, div-by-zero, "
    "signed-overflow, pointer-overflow, undefined-shift), plus every assertion of the inductive harness and the "
    "loop invariant, assigns and decreases clauses of every contracted loop; not checked by this gate (bounded "
    "CBMC result only): unsigned-overflow",
    "with at most one call of a claimed function per path",
    "known gap (not detected): a memcpy, memmove or memset that overflows into a neighbouring field of the same object",
    "VERIFIED_INDUCTIVE is not PROVED",
)
SPONGE_PROBES = {
    "leanc_function:sponge_absorb",
    "leanc_return:sponge_absorb",
    "leanc_loop:src/sponge.c:9",
    "leanc_loop:src/sponge.c:35",
}
FAILED_INDUCTION = {
    (Reason.INDUCTIVE_STEP_FAILED.value, INDUCTIVE_GATE),
    (Reason.SUSPECTED_NON_TERMINATION.value, INDUCTIVE_GATE),
    (Reason.PROPERTY_UNPROVED.value, INDUCTIVE_GATE),
}
COPY_MODEL_DETAIL = "memcpy destination region writeable"
INDUCTIVE_COVERAGE_WORDS = ("inductive gate", "inductive harness", "inductive proof", "by induction")
TIMEOUT_SPEC_LINE = ("  timeout_s: 600\n", "  timeout_s: 8\n")
TIMEOUT_S = 8
ISET_SOURCE = "src/iset.c"
ISET_SCOPE = (
    "claimed functions: iset_insert(); functions that are not claimed have only the bounded CBMC result",
    "contracted loops proved by induction (invariant at entry and after one step, assigns, decreases): "
    "src/iset.c:11 in lower_bound(), src/iset.c:92 in iset_insert()",
    "harness assumptions of harness/inductive.c (executions outside them are not covered): "
    "harness/inductive.c:12: 0 <= s.len && s.len <= s.cap && s.cap <= 0x7fffffff; "
    "harness/inductive.c:14: s.items != ((void *)0)",
)
ISET_TERMINATION_LINE = 11
SIGNED_OVERFLOW_DETAIL = "arithmetic overflow on signed + in lo + hi"
ISET_TIMEOUT_S = 600


def _verified(capsys, root: Path) -> tuple[int, dict]:
    return cli(capsys, "verify", str(approved(capsys, root)))


def _assert_scope(payload: dict, statements: tuple[str, ...]) -> None:
    assumptions = payload["verification"]["assumptions"]
    for statement in statements:
        assert any(statement in assumption for assumption in assumptions), statement
    for name, report in report_texts(payload).items():
        flat_report = flat(report)
        assert f"`{Status.VERIFIED_INDUCTIVE.value}`** (exit code 0)" in flat_report, name
        for statement in statements:
            assert flat(statement) in flat_report, (name, statement)


def _states_inductive_coverage(text: str) -> bool:
    return any(word in text for word in INDUCTIVE_COVERAGE_WORDS)


def _assert_unproved_without_claim(payload: dict, outcome: Outcome) -> None:
    verification = payload["verification"]
    assert {failure["kind"] for failure in verification["failures"]} <= {"unproved"}
    assert all(failure["inputs"] == [] for failure in verification["failures"])
    statement = no_claim_statement(outcome.value)
    mentioning = [text for text in verification["assumptions"] if _states_inductive_coverage(text)]
    assert mentioning and all(text.startswith(statement) for text in mentioning), mentioning
    assert not claims_anything(verification)
    for name, report in report_texts(payload).items():
        assert statement in flat(report), name
        assert not mentions_claim(report), name


def _codes_of(verification: dict, reason: Reason) -> list[dict]:
    return [code for code in verification["reason_codes"] if code["code"] == reason.value]


def _locations(codes: list[dict]) -> list[tuple[str, int]]:
    return [(code["location"]["file"], code["location"]["line"]) for code in codes]


def test_the_fixed_sponge_example_is_verified_inductive_within_its_stated_scope(example_copy, capsys):
    rc, payload = _verified(capsys, example_copy(EXAMPLE))
    verification = payload["verification"]
    assert verification["status"] == Status.VERIFIED_INDUCTIVE.value, verification["reasons"]
    assert rc == exit_code(Status.VERIFIED_INDUCTIVE) == 0
    stages = stages_by_name(verification)
    assert list(stages) == SPONGE_GATES
    assert {stage["outcome"] for stage in stages.values()} == {Outcome.PASS.value}
    assert verification["reason_codes"] == [] and verification["failures"] == []
    _assert_scope(payload, SPONGE_SCOPE)
    details = inductive_record(payload)["details"]
    assert details["scope"]["functions"] == ["sponge_absorb"]
    inserted = details["vacuity"]["inserted"]
    assert set(inserted) == SPONGE_PROBES
    assert all(probe["reached"] >= probe["expected"] >= 1 for probe in inserted.values())
    assert details["vacuity"]["probes"] == {"absorb_returned": True}


def test_the_buggy_sponge_passes_the_bounded_gate_and_is_unknown_with_the_inductive_codes(example_copy, capsys):
    root = example_copy(EXAMPLE)
    shutil.copyfile(BUGGY_SPONGE, root / SPONGE_SOURCE)
    rc, payload = _verified(capsys, root)
    verification = payload["verification"]
    assert verification["status"] == Status.UNKNOWN.value
    assert rc == exit_code(Status.UNKNOWN) and not verification["success"]
    stages = stages_by_name(verification)
    assert stages["cbmc"]["outcome"] == Outcome.PASS.value
    assert stages["sanitizer"]["outcome"] == Outcome.PASS.value
    assert stages[INDUCTIVE_GATE]["outcome"] == Outcome.INCONCLUSIVE.value
    assert reason_pairs(verification) == FAILED_INDUCTION
    termination = _codes_of(verification, Reason.SUSPECTED_NON_TERMINATION)
    assert _locations(termination) == [(SPONGE_SOURCE, ABSORB_LOOP_LINE)]
    step = _codes_of(verification, Reason.INDUCTIVE_STEP_FAILED)
    assert _locations(step) == [(SPONGE_SOURCE, ABSORB_LOOP_LINE)]
    assert any(COPY_MODEL_DETAIL in code["detail"] for code in _codes_of(verification, Reason.PROPERTY_UNPROVED))
    assert verification["failures"]
    _assert_unproved_without_claim(payload, Outcome.INCONCLUSIVE)


def test_a_timeout_of_the_claimed_target_is_a_timeout_and_never_a_partial_pass(example_copy, capsys):
    root = example_copy(EXAMPLE)
    spec = root / SPEC_FILE
    text = spec.read_text(encoding="utf-8")
    assert TIMEOUT_SPEC_LINE[0] in text
    spec.write_text(text.replace(*TIMEOUT_SPEC_LINE), encoding="utf-8")
    assert load_spec(spec).verification.timeout_s == TIMEOUT_S
    rc, payload = _verified(capsys, root)
    verification = payload["verification"]
    stages = stages_by_name(verification)
    assert stages["cbmc"]["outcome"] == Outcome.PASS.value, stages["cbmc"]["summary"]
    inductive = stages[INDUCTIVE_GATE]
    assert inductive["outcome"] == Outcome.TIMEOUT.value and "timed out" in inductive["summary"]
    assert verification["status"] == Status.TIMEOUT.value
    assert rc == exit_code(Status.TIMEOUT) and not is_success(Status(verification["status"]))
    assert (Reason.GATE_TIMEOUT.value, INDUCTIVE_GATE) in reason_pairs(verification)
    record = inductive_record(payload)
    assert record["details"]["timeout_s_per_run"] == TIMEOUT_S
    assert any(invocation["timed_out"] for invocation in record["invocations"])
    _assert_unproved_without_claim(payload, Outcome.TIMEOUT)


def test_the_fixed_iset_claiming_its_insert_target_is_verified_inductive(tmp_path, capsys):
    rc, payload = _verified(capsys, iset_project(tmp_path, "fixed", claim=INSERT_CLAIM))
    verification = payload["verification"]
    assert verification["status"] == Status.VERIFIED_INDUCTIVE.value, verification["reasons"]
    assert rc == 0 and verification["reason_codes"] == []
    _assert_scope(payload, ISET_SCOPE)
    record = inductive_record(payload)
    assert record["details"]["scope"]["functions"] == ["iset_insert"]
    assert record["details"]["timeout_s_per_run"] == ISET_TIMEOUT_S
    assert not any(invocation["timed_out"] for invocation in record["invocations"])


def test_the_buggy_iset_claiming_its_insert_target_is_unknown_with_the_inductive_codes(tmp_path, capsys):
    rc, payload = _verified(capsys, iset_project(tmp_path, "buggy", claim=INSERT_CLAIM))
    verification = payload["verification"]
    assert verification["status"] == Status.UNKNOWN.value
    assert rc == exit_code(Status.UNKNOWN)
    assert stages_by_name(verification)["cbmc"]["outcome"] == Outcome.PASS.value
    assert reason_pairs(verification) == FAILED_INDUCTION
    termination = _codes_of(verification, Reason.SUSPECTED_NON_TERMINATION)
    assert _locations(termination) == [(ISET_SOURCE, ISET_TERMINATION_LINE)]
    assert any(SIGNED_OVERFLOW_DETAIL in code["detail"] for code in verification["reason_codes"])
    _assert_unproved_without_claim(payload, Outcome.INCONCLUSIVE)
