from __future__ import annotations

import json
from pathlib import Path

import pytest
from reports_support import _run_record, _section, _vr

from leanc.cli import main
from leanc.foundation.models import FailureReport, Location, ReasonCode, StageResult
from leanc.foundation.reasons import Reason
from leanc.foundation.status import SUCCESS_STATUSES, Outcome, Status, is_success, outcome_to_status
from leanc.pipeline import controller
from leanc.pipeline.controller import verify
from leanc.reports.json_report import dumps, read_verification, summary, write_json
from leanc.reports.markdown import render_run_md, render_verification_md
from leanc.reports.markdown.context_sections import REASON_CODES_HEADING

PLAIN_EXAMPLE = "safe_array_get"
SUMMARY_KEYS_BEFORE_REASON_CODES = {
    "run_id",
    "status",
    "exit_code",
    "success",
    "reasons",
    "spec_id",
    "spec_version",
    "spec_hash",
    "approval",
    "stages",
    "failures",
    "detected_by",
    "assumptions",
    "limitations",
}
UNWIND_LOCATION = Location(file="src/safe_array_get.c", line=9, function="safe_array_get")
UNWIND_FAILURE = FailureReport(
    id="cbmc:safe_array_get.unwind.0",
    kind="unwind",
    tool="cbmc",
    property_id="safe_array_get.unwind.0",
    description="unwinding assertion loop 0",
    location=UNWIND_LOCATION,
)
MARKUP_DETAIL = "f() *returns* `x` | <b>"


@pytest.mark.parametrize("status", list(Status))
def test_summary_adds_reason_codes_and_keeps_every_existing_key(status):
    vr = _vr(status)
    data = summary(vr)
    assert set(data) == SUMMARY_KEYS_BEFORE_REASON_CODES | {"reason_codes"}
    assert data["reason_codes"] == [code.model_dump(mode="json") for code in vr.reason_codes]
    assert bool(data["reason_codes"]) is not is_success(status)


def test_summary_reason_code_entries_are_plain_json():
    entry = summary(_vr(Status.UNKNOWN))["reason_codes"]
    assert entry and set(entry[0]) == {"code", "stage", "location", "detail"}
    assert {(e["code"], e["stage"]) for e in entry} >= {("gate_skipped", "sanitizer"), ("unwind_insufficient", "cbmc")}
    json.dumps(entry)


def test_reason_codes_survive_a_json_roundtrip(tmp_path):
    vr = _vr(Status.UNKNOWN)
    write_json(vr, tmp_path / "verification.json")
    back = read_verification(tmp_path / "verification.json")
    assert back.reason_codes == vr.reason_codes
    assert all(isinstance(code.code, Reason) for code in back.reason_codes)


def test_a_verification_written_before_reason_codes_still_loads(tmp_path):
    data = json.loads(dumps(_vr(Status.COUNTEREXAMPLE_FOUND)))
    del data["reason_codes"]
    (tmp_path / "verification.json").write_text(json.dumps(data))
    assert read_verification(tmp_path / "verification.json").reason_codes == []


def test_verification_md_lists_the_reason_codes_under_the_verdict():
    vr = _vr(Status.UNKNOWN)
    verdict = _section(render_verification_md(vr), "Verdict")
    assert REASON_CODES_HEADING in verdict
    assert "- `gate_skipped` (`sanitizer`): skipped: no sandbox" in verdict
    assert "- `unwind_insufficient` (`cbmc`): main.unwind.0: unwinding assertion loop 0" in verdict


@pytest.mark.parametrize("status", sorted(SUCCESS_STATUSES))
def test_verification_md_of_a_success_has_no_reason_codes(status):
    assert REASON_CODES_HEADING not in render_verification_md(_vr(status))


def test_reason_code_line_shows_the_location_and_escapes_the_detail():
    code = ReasonCode(code=Reason.UNWIND_INSUFFICIENT, stage="cbmc", location=UNWIND_LOCATION, detail=MARKUP_DETAIL)
    verdict = _section(render_verification_md(_vr(Status.UNKNOWN, reason_codes=[code])), "Verdict")
    line = next(line for line in verdict.splitlines() if line.startswith("- `unwind_insufficient`"))
    assert "`src/safe_array_get.c:9 in safe_array_get()`" in line
    assert "\\*returns\\*" in line and "\\`x\\`" in line and "\\<b>" in line


def test_run_md_lists_the_final_reason_codes():
    record = _run_record()
    record.verification = _vr(Status.COUNTEREXAMPLE_FOUND)
    record.status = Status.COUNTEREXAMPLE_FOUND
    assert "- `gate_failed` (`cbmc`)" in _section(render_run_md(record), "Verdict")


def _approved_example(example_copy) -> Path:
    root = example_copy(PLAIN_EXAMPLE)
    assert main(["spec", "approve", str(root / "spec.yaml"), "--by", "reviewer", "--yes"]) == 0
    return root


def _stub_gates(monkeypatch, cbmc: StageResult) -> None:
    def runner(gate: str):
        def run(ctx) -> StageResult:
            return cbmc if gate == "cbmc" else StageResult(stage=gate, outcome=Outcome.PASS, summary="stub pass")

        return run

    for gate in list(controller._GATE_RUNNERS):
        monkeypatch.setitem(controller._GATE_RUNNERS, gate, runner(gate))


def test_verify_records_codes_derived_from_the_stages(example_copy, tmp_path, monkeypatch, capsys):
    root = _approved_example(example_copy)
    cbmc = StageResult(
        stage="cbmc",
        outcome=Outcome.INCONCLUSIVE,
        status=outcome_to_status("cbmc", Outcome.INCONCLUSIVE),
        summary="unwind bound 5 is insufficient",
        failures=[UNWIND_FAILURE],
    )
    _stub_gates(monkeypatch, cbmc)
    result = verify(root / "spec.yaml", project_root=root, run_dir=tmp_path / "run")
    assert result.status is Status.UNKNOWN
    assert [(code.code, code.stage, code.location) for code in result.reason_codes] == [
        (Reason.UNWIND_INSUFFICIENT, "cbmc", UNWIND_LOCATION)
    ]
    assert read_verification(tmp_path / "run").reason_codes == result.reason_codes
    assert "`unwind_insufficient`" in (tmp_path / "run" / "verification.md").read_text()


def test_verify_of_a_passing_run_records_no_code(example_copy, tmp_path, monkeypatch, capsys):
    root = _approved_example(example_copy)
    _stub_gates(monkeypatch, StageResult(stage="cbmc", outcome=Outcome.PASS, summary="stub pass"))
    result = verify(root / "spec.yaml", project_root=root, run_dir=tmp_path / "run")
    assert result.status is Status.VERIFIED_BOUNDED
    assert result.reason_codes == []


def test_verify_json_of_an_unapproved_spec_carries_the_spec_code(example_copy, capsys):
    root = example_copy(PLAIN_EXAMPLE)
    rc = main(["verify", str(root / "spec.yaml"), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 40 and payload["status"] == Status.SPEC_UNAPPROVED.value
    codes = payload["verification"]["reason_codes"]
    assert [(c["code"], c["stage"]) for c in codes] == [(Reason.GATE_FAILED.value, "spec")]
    assert codes[0]["detail"].startswith("spec is not approved")
