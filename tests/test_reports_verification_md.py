from __future__ import annotations

import re

import pytest
from reports_support import (
    HASH,
    _cbmc_failure,
    _env,
    _headings,
    _inv,
    _section,
    _stage,
    _static_failure,
    _vr,
)

from leanc.foundation.models import Diagnostic, TraceStep
from leanc.foundation.status import EXIT_CODES, Outcome, Status
from leanc.reports.markdown import (
    ALWAYS_LIMITATIONS,
    STATUS_MEANING,
    render_verification_md,
)


def test_every_status_has_a_meaning():
    assert set(STATUS_MEANING) == set(Status)
    assert all(len(v) > 40 for v in STATUS_MEANING.values())


def test_meanings_are_honest():
    vb = STATUS_MEANING[Status.VERIFIED_BOUNDED]
    for phrase in ("harness", "input constraints", "unwind bound", "checked property classes", "not", "PROVED"):
        assert phrase in vb
    assert "never equal to PROVED" in vb
    proved = STATUS_MEANING[Status.PROVED]
    for phrase in ("Frama-C/WP", "APPROVED ACSL contract", "runtime-error", "smoke tests", "partial correctness",
                   "CBMC never produces"):
        assert phrase in proved
    unk = STATUS_MEANING[Status.UNKNOWN]
    assert "unwinding" in unk and "vacuous" in unk and "skipped" in unk and "NOT evidence of correctness" in unk
    assert "Nothing was formally verified against a trusted spec" in STATUS_MEANING[Status.SPEC_UNAPPROVED]


SECTIONS = ["Verdict", "Scope & assumptions", "Specification & approval", "Environment", "Gates",
            "Findings by detecting tool", "Counterexamples", "Limitations", "Reproduce"]


@pytest.mark.parametrize("status", list(Status))
def test_verification_md_every_status(status):
    vr = _vr(status)
    md = render_verification_md(vr)
    assert _headings(md) == SECTIONS
    verdict = _section(md, "Verdict")
    assert f"**Status: `{status.value}`** (exit code {EXIT_CODES[status]})" in verdict
    assert STATUS_MEANING[status] in verdict
    for r in vr.reasons:
        assert r.split(" ")[0].strip("[]") in verdict
    lim = _section(md, "Limitations")
    for item in ALWAYS_LIMITATIONS:
        assert item in lim
    assert "Properties that are not encoded as assertions" in lim
    assert lim.count("Sanitizer") == 1 and lim.count("CBMC results are bounded") == 1
    assert md == render_verification_md(vr.model_copy(deep=True))
    assert md.endswith("\n") and "\n\n\n" not in md


def test_verification_md_spec_unapproved():
    md = render_verification_md(_vr(Status.SPEC_UNAPPROVED))
    spec = _section(md, "Specification & approval")
    assert "**NOT approved**" in spec and "nothing in this report was formally verified" in spec
    assert "harness/h.c was modified since approval" in spec
    assert "No result in this report is a verification result" in _section(md, "Verdict")


def test_verification_md_approved_spec_and_environment():
    md = render_verification_md(_vr(Status.VERIFIED_BOUNDED))
    spec = _section(md, "Specification & approval")
    assert "`safe-array-get`, version 2" in spec and HASH in spec
    assert "**approved** by `alice` at 2026-11-01T10:00:00+00:00" in spec and "signature valid: yes" in spec
    assert "`include/safe_array_get.h`" in spec
    env = _section(md, "Environment")
    assert "Sandbox: `bwrap`: bubblewrap: no network" in env
    assert "| `cbmc` | yes | 5.95.1 | `/usr/bin/cbmc` |" in env
    scope = _section(md, "Scope & assumptions")
    assert "holds **only** within this scope" in scope
    assert "unwind bound 5 for all loops" in scope and "`src/safe_array_get.c`" in scope
    assert "len <= 4, idx unconstrained" in scope
    assert "never equal to PROVED" in _section(md, "Verdict")


def test_verification_md_no_sandbox_is_flagged():
    vr = _vr(Status.UNKNOWN, environment=_env("none"))
    env = _section(render_verification_md(vr), "Environment")
    assert "Sandbox: `none`" in env and "No sandbox was active" in env


def test_gates_table():
    md = render_verification_md(_vr(Status.UNKNOWN))
    gates = _section(md, "Gates")
    assert "| stage | outcome | status | detecting tools | duration | summary |" in gates
    assert "| `spec` | PASS | - | - | 0.50 s |" in gates
    assert "| `compile` | PASS | - | `clang` | 0.50 s |" in gates
    assert "| `sanitizer` | **SKIPPED** | `UNKNOWN` | - |" in gates
    assert "| `cbmc` | **INCONCLUSIVE** | `UNKNOWN` | `cbmc` |" in gates


def test_findings_grouped_by_detecting_tool():
    vr = _vr(Status.RUNTIME_TEST_FAILED)
    vr.stages[1].failures.append(_static_failure().model_copy(update={"tool": "clang-static-analyzer"}))
    vr.stages.append(_stage("cbmc", Outcome.FAIL, failures=[_cbmc_failure()], required=False))
    md = render_verification_md(vr)
    findings = _section(md, "Findings by detecting tool")
    groups = re.findall(r"^### `([^`]+)`", findings, re.M)
    assert groups == ["clang", "clang-static-analyzer", "asan", "ubsan", "test-oracle", "cbmc"]
    clang = findings.split("### `clang` ")[1].split("###")[0]
    assert "warning (non-blocking) at `src/safe_array_get.c:4:9`: unused variable 'tmp'" in clang
    assert "declared here" not in findings
    assert "note-level diagnostic" in findings
    asan = findings.split("### `asan` (AddressSanitizer): 1 failure")[1].split("###")[0]
    assert "**runtime** at `src/safe_array_get.c:12:9 in safe_array_get()`: heap-buffer-overflow READ of size 4" in asan
    assert "property `heap-buffer-overflow`" in asan and "raw output `raw/sanitizer-case-oob.txt`" in asan
    assert "(optional)" in _section(md, "Gates")


def test_diagnostics_of_a_gate_without_verdict_are_not_called_non_blocking():
    vr = _vr(Status.UNKNOWN)
    unmodelled = Diagnostic(tool="cbmc", severity="warning", message="no body for function memchr", rule_id="no-body")
    vr.stages.append(_stage("cbmc-extra", Outcome.INCONCLUSIVE, diagnostics=[unmodelled], required=False))
    findings = _section(render_verification_md(vr), "Findings by detecting tool")
    cbmc = findings.split("### `cbmc` ")[1].split("###")[0]
    assert "warning (stage INCONCLUSIVE) at" in cbmc and "non-blocking" not in cbmc


def test_static_failure_not_duplicated_by_its_diagnostic():
    findings = _section(render_verification_md(_vr(Status.STATIC_ANALYSIS_WARNING)), "Findings by detecting tool")
    sa = findings.split("### `clang-static-analyzer` (Clang Static Analyzer): 1 failure\n")[1].split("###")[0]
    assert sa.count("Dereference of null pointer") == 1 and "non-blocking" not in sa


def test_no_findings_message_is_honest():
    vr = _vr(Status.VERIFIED_BOUNDED)
    vr.stages[1].diagnostics = []
    findings = _section(render_verification_md(vr), "Findings by detecting tool")
    assert "does not mean the code is correct" in findings


def test_counterexample_section():
    md = render_verification_md(_vr(Status.COUNTEREXAMPLE_FOUND))
    ce = _section(md, "Counterexamples")
    assert "### 1. `safe_array_get.pointer_dereference.7` (`cbmc`)" in ce
    assert "Location: `src/safe_array_get.c:12:9 in safe_array_get()`" in ce
    assert "| `idx` | `4294967295u` | `unsigned int` |" in ce
    assert "| `len` | `3` | `size_t` |" in ce
    assert "| `weird\\|name` | `a\\|b c` | - |" in ce
    assert "1. `main`\n2. `safe_array_get`" in ce
    assert "last 15 of 31 steps" in ce
    assert "#31 failure src/safe_array_get.c:12: pointer outside object bounds" in ce
    assert "#17 assignment harness/h.c:17: step_17 = 17" in ce and "#16 assignment" not in ce
    assert "Raw tool output: `raw/cbmc-verify.json`" in ce
    assert "Explanation: idx is not checked against len" in ce
    assert "arr[(signed long int)idx]" in ce


def test_runtime_failures_listed_as_failing_executions():
    ce = _section(render_verification_md(_vr(Status.RUNTIME_TEST_FAILED)), "Counterexamples")
    assert "(`asan`)" in ce and "(`test-oracle`)" in ce
    assert "(`ubsan`)" not in ce
    assert "1. `safe_array_get src/safe_array_get.c:12`" in ce


def test_reproduce_section():
    md = render_verification_md(_vr(Status.VERIFIED_BOUNDED))
    rep = _section(md, "Reproduce")
    assert "```sh\ncd /runs/r1/snapshot && clang -std=c17 -Wall -c src/safe_array_get.c -o /b/x.o\n```" in rep
    assert "cbmc harness/h.c src/safe_array_get.c --unwind 5 --json-ui" in rep
    assert "stdout `logs/05-cbmc.stdout.txt`" in rep
    vr = _vr(Status.UNKNOWN)
    vr.stages[1].invocations = [_inv("clang", ["clang", "-DMSG=a b", "x;rm -rf /"], "01")]
    rep = _section(render_verification_md(vr), "Reproduce")
    assert "clang '-DMSG=a b' 'x;rm -rf /'" in rep


def test_table_cells_and_inline_text_are_escaped():
    vr = _vr(Status.TOOL_ERROR)
    vr.stages[-1].summary = "cbmc crashed | exit 134\nsee `log` <stdin> *bold* [x](http://evil)"
    vr.reasons = ["# fake heading", "> quote", "1. ordered"]
    md = render_verification_md(vr)
    row = next(line for line in _section(md, "Gates").splitlines() if line.startswith("| `cbmc`"))
    assert row.count(" | ") == 5
    assert "cbmc crashed \\| exit 134 see \\`log\\` \\<stdin> \\*bold\\* [x\\](http://evil)" in row
    verdict = _section(md, "Verdict")
    assert "- \\# fake heading" in verdict and "- \\> quote" in verdict and "- 1\\. ordered" in verdict
    assert not re.search(r"^# fake", md, re.M)


def test_fenced_blocks_cannot_be_escaped():
    vr = _vr(Status.COUNTEREXAMPLE_FOUND)
    f = vr.stages[-1].failures[0]
    f.trace = [TraceStep(kind="output", text="```\n## injected")]
    ce = _section(render_verification_md(vr), "Counterexamples")
    assert "````text\n#1 output: ``` ## injected\n````" in ce
