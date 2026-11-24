from __future__ import annotations

import json
from pathlib import Path

import pytest
from inductive_iset_support import SOURCE, approved, cli, iset_project

from leanc.foundation.models import StageResult
from leanc.foundation.reasons import Reason
from leanc.foundation.status import Outcome, Status, exit_code
from leanc.pipeline import controller
from leanc.pipeline.controller import INDUCTIVE_LIMITATION, INTEGRITY_FAILED, verify
from leanc.specs.model import INDUCTIVE_GATE

pytestmark = pytest.mark.usefixtures("needs_toolchain")

GATES_IN_ORDER = ["spec", "compile", "integrity", "static_analysis", "cbmc", INDUCTIVE_GATE]
VERIFICATION_REPORT = Path("verify") / "verification.md"
VERIFICATION_RECORD = Path("verify") / "verification.json"
SANDBOX = "bwrap"
SCOPE_STATEMENTS = (
    "claimed functions: iset_rank(), iset_contains(); functions that are not claimed have only the bounded CBMC result",
    "contracted loops proved by induction (invariant at entry and after one step, assigns, decreases): "
    "src/iset.c:11 in lower_bound()",
    "every execution of main() of the protected inductive harness harness/inductive.c",
    "with at most one call of a claimed function per path",
    "harness assumptions of harness/inductive.c (executions outside them are not covered): "
    "harness/inductive.c:12: 0 <= s.len && s.len <= s.cap && s.cap <= 0x7fffffff",
    "checked property classes: the mandatory CBMC check classes only (bounds, pointer, div-by-zero, "
    "signed-overflow, pointer-overflow, undefined-shift)",
    "known gap (not detected): a memcpy, memmove or memset that overflows into a neighbouring field of the same object",
    "VERIFIED_INDUCTIVE is not PROVED",
)
CLAIM_MARKERS = ("claimed functions:", "contracted loops proved by induction")
NO_CLAIM_STATEMENT = "the inductive gate ended INCONCLUSIVE: it establishes no inductive claim"
BUGGY_CODES = {
    (Reason.INDUCTIVE_STEP_FAILED.value, INDUCTIVE_GATE),
    (Reason.SUSPECTED_NON_TERMINATION.value, INDUCTIVE_GATE),
    (Reason.PROPERTY_UNPROVED.value, INDUCTIVE_GATE),
}
TERMINATION_CODE_LINE = "`suspected_non_termination` (`inductive`, `src/iset.c:11 in lower_bound()`)"
OVERFLOW_DETAIL = "arithmetic overflow on signed + in lo + hi"
TERMINATING_CALL = "    return lower_bound(s->items, s->len, x);\n}\n\nint iset_len"
ABORTING_CALL = "    if (x == 7) {\n        abort();\n    }\n" + TERMINATING_CALL


def _verified(capsys, root: Path) -> tuple[int, dict]:
    return cli(capsys, "verify", str(approved(capsys, root)))


def _stages(verification: dict) -> dict[str, dict]:
    return {stage["stage"]: stage for stage in verification["stages"]}


def _reports(payload: dict) -> dict[str, str]:
    run_report = Path(payload["report"])
    return {
        "run report": run_report.read_text(encoding="utf-8"),
        "verification report": (Path(payload["run_dir"]) / VERIFICATION_REPORT).read_text(encoding="utf-8"),
    }


def _inductive_record(payload: dict) -> dict:
    record = json.loads((Path(payload["run_dir"]) / VERIFICATION_RECORD).read_text(encoding="utf-8"))
    return next(stage for stage in record["stages"] if stage["stage"] == INDUCTIVE_GATE)


def _flat(text: str) -> str:
    return " ".join(text.replace("\\*", "*").split())


def test_fixed_iset_is_verified_inductive_through_leanc_verify(tmp_path, capsys):
    rc, payload = _verified(capsys, iset_project(tmp_path, "fixed"))
    verification = payload["verification"]
    assert verification["status"] == Status.VERIFIED_INDUCTIVE.value, verification["reasons"]
    assert rc == exit_code(Status.VERIFIED_INDUCTIVE) == 0 and verification["success"]
    stages = _stages(verification)
    assert list(stages) == GATES_IN_ORDER
    assert {stage["outcome"] for stage in stages.values()} == {Outcome.PASS.value}
    assert verification["reason_codes"] == [] and verification["failures"] == []
    assumptions = verification["assumptions"]
    for statement in SCOPE_STATEMENTS:
        assert any(statement in assumption for assumption in assumptions), statement
    assert INDUCTIVE_LIMITATION in verification["limitations"]
    record = _inductive_record(payload)
    assert record["details"]["sandbox"] == SANDBOX
    assert {invocation["sandbox"] for invocation in record["invocations"]} == {SANDBOX}
    assert record["details"]["scope"]["functions"] == ["iset_rank", "iset_contains"]
    for name, report in _reports(payload).items():
        flat = _flat(report)
        assert f"`{Status.VERIFIED_INDUCTIVE.value}`** (exit code 0)" in flat, name
        for statement in SCOPE_STATEMENTS:
            assert _flat(statement) in flat, (name, statement)


def test_buggy_iset_passes_the_bounded_gate_and_is_unknown_with_the_inductive_codes(tmp_path, capsys):
    rc, payload = _verified(capsys, iset_project(tmp_path, "buggy"))
    verification = payload["verification"]
    assert verification["status"] == Status.UNKNOWN.value
    assert rc == exit_code(Status.UNKNOWN) and not verification["success"]
    stages = _stages(verification)
    assert stages["cbmc"]["outcome"] == Outcome.PASS.value
    assert stages[INDUCTIVE_GATE]["outcome"] == Outcome.INCONCLUSIVE.value
    assert stages[INDUCTIVE_GATE]["status"] == Status.UNKNOWN.value
    codes = verification["reason_codes"]
    assert {(code["code"], code["stage"]) for code in codes} == BUGGY_CODES
    termination = [code for code in codes if code["code"] == Reason.SUSPECTED_NON_TERMINATION.value]
    assert [(code["location"]["file"], code["location"]["line"]) for code in termination] == [(SOURCE, 11)]
    assert any(OVERFLOW_DETAIL in code["detail"] for code in codes)
    assert {failure["kind"] for failure in verification["failures"]} == {"unproved"}
    assert all(failure["inputs"] == [] for failure in verification["failures"])
    assert NO_CLAIM_STATEMENT in "\n".join(verification["assumptions"])
    assert not any(marker in assumption for marker in CLAIM_MARKERS for assumption in verification["assumptions"])
    for name, report in _reports(payload).items():
        assert f"`{Status.UNKNOWN.value}`** (exit code {exit_code(Status.UNKNOWN)})" in _flat(report), name
        assert TERMINATION_CODE_LINE in report, name
        assert NO_CLAIM_STATEMENT in _flat(report), name
        assert not any(marker in report for marker in CLAIM_MARKERS), name


def test_integrity_failure_skips_the_inductive_gate_with_gate_skipped(tmp_path, capsys, monkeypatch):
    root = iset_project(tmp_path, "fixed")
    source = root / SOURCE
    text = source.read_text(encoding="utf-8")
    assert TERMINATING_CALL in text
    source.write_text(text.replace(TERMINATING_CALL, ABORTING_CALL), encoding="utf-8")
    spec = approved(capsys, root)
    called: list[str] = []

    def tripwire(ctx) -> StageResult:
        called.append(INDUCTIVE_GATE)
        return StageResult(stage=INDUCTIVE_GATE, outcome=Outcome.PASS, summary="must not run")

    monkeypatch.setitem(controller._GATE_RUNNERS, INDUCTIVE_GATE, tripwire)
    result = verify(spec, project_root=root, run_dir=tmp_path / "run")
    stages = {stage.stage: stage for stage in result.stages}
    assert stages["integrity"].outcome is Outcome.FAIL
    assert called == []
    inductive = stages[INDUCTIVE_GATE]
    assert inductive.outcome is Outcome.SKIPPED and INTEGRITY_FAILED in inductive.summary
    assert (Reason.GATE_SKIPPED, INDUCTIVE_GATE) in {(code.code, code.stage) for code in result.reason_codes}
    assert result.status is not Status.VERIFIED_INDUCTIVE and not any(
        stage.outcome is Outcome.PASS for stage in (stages["cbmc"], inductive)
    )
