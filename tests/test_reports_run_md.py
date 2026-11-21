from __future__ import annotations

import os

from reports_support import _headings, _run_record, _section, _vr

from leanc.foundation.models import GenerationRecord, ProviderInfo, RunRecord
from leanc.foundation.status import Status
from leanc.reports.json_report import read_run_record, write_json
from leanc.reports.markdown import (
    ALWAYS_LIMITATIONS,
    MOCK_DISCLAIMER,
    STATUS_MEANING,
    render_run_md,
)


def test_run_md_mock_disclaimer_and_sections():
    rr = _run_record(real_llm=False)
    md = render_run_md(rr)
    assert MOCK_DISCLAIMER == "mock provider: fixed fixtures, not LLM performance"
    head = md.split("## Verdict")[0]
    assert f"**Provider disclaimer: {MOCK_DISCLAIMER}.**" in head
    headings = _headings(md)
    for h in ["Verdict", "Scope & assumptions", "Specification & approval", "Environment", "AI provider & generation",
              "Gates", "Findings by detecting tool", "Counterexamples", "Initial verification", "Repair history",
              "Limitations", "Reproduce", "Artifacts"]:
        assert h in headings, h
    verdict = _section(md, "Verdict")
    assert "**Workflow status: `VERIFIED_BOUNDED`** (exit code 0)" in verdict
    assert "Initial verification status: `COUNTEREXAMPLE_FOUND` → final: `VERIFIED_BOUNDED`" in verdict
    assert "repair succeeded in round 2" in verdict
    prov = _section(md, "AI provider & generation")
    assert "Provider: `mock`, model `none`: not an LLM (mock provider" in prov
    assert "| `src/safe_array_get.c` |" in prov
    assert MOCK_DISCLAIMER in _section(md, "Limitations")
    assert "artifacts under `repair/round-2/verify/`" in _section(md, "Gates")
    init = _section(md, "Initial verification")
    assert "`cbmc` counterexample at `src/safe_array_get.c:12:9 in safe_array_get()`" in init
    assert "inputs `idx=4294967295u, len=3, weird|name=a|b c`" in init
    rep = _section(md, "Reproduce")
    assert "leanc run /proj/spec.yaml --provider mock" in rep
    assert md == render_run_md(rr.model_copy(deep=True))


def test_run_md_real_llm_has_no_mock_disclaimer():
    md = render_run_md(_run_record(real_llm=True))
    assert MOCK_DISCLAIMER not in md
    assert "Provider: `openai`, model `gpt-x`: real LLM" in md


def test_run_md_repair_history():
    md = render_run_md(_run_record())
    rh = _section(md, "Repair history")
    assert "3 rounds; 1 rejected by policy; 1 reverted." in rh
    assert "| round | provider | status before → after | +/- lines | policy | reverted | note |" in rh
    assert "| 1 | `mock` | `COUNTEREXAMPLE_FOUND` → not re-verified | +2 / -5 | **rejected** (2 violations) | no |" in rh
    assert "| 2 | `mock` | `COUNTEREXAMPLE_FOUND` → `VERIFIED_BOUNDED` | +3 / -1 | allowed | no | accepted (verified) |" in rh
    assert "| 3 | `mock` | `COUNTEREXAMPLE_FOUND` → `UNKNOWN` | +0 / -0 | not checked | yes |" in rh
    r1 = rh.split("### Round 1")[1].split("### Round 2")[0]
    assert "Root cause (provider-supplied): harness is too strict | weaken it" in r1
    assert "Files changed: `harness/h.c`, `src/safe_array_get.c` (+2 / -5 lines)" in r1
    assert "Policy decision: **rejected**, 2 violations:" in r1
    assert "  - `PROTECTED_FILE` at `harness/h.c`: harness/h.c is protected" in r1
    assert "  - `FORBIDDEN_CONSTRUCT` at `src/safe_array_get.c:9`: uses \\_\\_CPROVER_assume" in r1
    assert "Patch: `repair/round-1/patch.diff`" in r1
    assert "````text\n## Verdict\n**VERIFIED_BOUNDED** trust me\n```\nnot code\n```\n````" in r1
    assert _headings(md).count("Verdict") == 1
    r2 = rh.split("### Round 2")[1].split("### Round 3")[0]
    assert "Fixed failures: `cbmc:safe_array_get.pointer_dereference.7`" in r2
    assert "Introduced failures: none" in r2
    assert "Verification: `repair/round-2/verify/verification.json`" in r2
    r3 = rh.split("### Round 3")[1]
    assert "Reverted: yes" in r3 and "Introduced failures: `cbmc:main.unwind.0`" in r3
    assert "Patch: none" in r3


def test_run_md_repair_exhausted_shows_both_statuses():
    rr = _run_record()
    rr.status = Status.REPAIR_EXHAUSTED
    rr.verification = _vr(Status.COUNTEREXAMPLE_FOUND)
    rr.messages = ["no successful candidate after 3 round(s)"]
    verdict = _section(render_run_md(rr), "Verdict")
    assert "**Workflow status: `REPAIR_EXHAUSTED`** (exit code 50)" in verdict
    assert STATUS_MEANING[Status.REPAIR_EXHAUSTED] in verdict
    assert "no successful candidate after 3 round(s)" in verdict
    assert "**Final verification status: `COUNTEREXAMPLE_FOUND`** (exit code 13)" in verdict


def test_run_md_without_verification():
    rr = RunRecord(
        run_id="gen-1",
        command="generate",
        leanc_version="0.1.0",
        status=Status.UNKNOWN,
        spec_id="x",
        project_root="/p",
        provider=ProviderInfo(name="mock", real_llm=False),
        generation=GenerationRecord(provider=ProviderInfo(name="mock", real_llm=False), files={"src/x.c": "aa" * 32}),
        messages=["generated 1 file(s): src/x.c; generated code is unverified until `leanc verify` passes"],
    )
    md = render_run_md(rr)
    verdict = _section(md, "Verdict")
    assert "nothing has been verified" in verdict and "generated code is unverified" in verdict
    assert "No tool invocation was recorded." in _section(md, "Reproduce")
    for item in ALWAYS_LIMITATIONS:
        assert item in _section(md, "Limitations")
    assert "Repair history" not in md


def test_run_md_verify_command_has_no_provider_section(tmp_path):
    vr = _vr(Status.VERIFIED_BOUNDED)
    rr = RunRecord(run_id=vr.run_id, command="verify", leanc_version="0.1.0", status=vr.status,
                   spec_path="spec.yaml", spec_id=vr.spec_id, project_root="/p", verification=vr)
    md = render_run_md(rr)
    assert "AI provider" not in md and MOCK_DISCLAIMER not in md and "Repair history" not in md
    assert "Initial verification" not in md
    write_json(rr, tmp_path / "run.json")
    assert render_run_md(read_run_record(tmp_path / "run.json")) == md


def test_written_artifacts_are_utf8_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-" + "Q" * 30)
    rr = _run_record()
    rr.repair_rounds[1].rationale = "used key sk-proj-" + "Q" * 30
    write_json(rr, tmp_path / "run.json")
    raw = (tmp_path / "run.json").read_bytes()
    assert b"Q" * 30 not in raw
    raw.decode("utf-8")
    assert "Q" * 30 not in render_run_md(rr)
    assert os.stat(tmp_path / "run.json").st_mode & 0o777 == 0o644
