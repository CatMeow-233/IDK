from __future__ import annotations

import shlex
from pathlib import PurePosixPath

from leanc.foundation.models import (
    FailureReport,
    RepairRound,
    RunRecord,
    VerificationResult,
)
from leanc.reports.markdown.closing_sections import (
    limitations_section,
    reproduce_section,
)
from leanc.reports.markdown.context_sections import (
    environment_section,
    reason_code_lines,
    scope_section,
    spec_section,
    verdict_lines,
)
from leanc.reports.markdown.evidence_sections import (
    GATE_TABLE_HEADER,
    counterexamples_section,
    coverage_section,
    findings_section,
    gate_rows,
    gates_section,
)
from leanc.reports.markdown.markup import (
    LIMITS,
    code_span,
    escape_inline,
    fenced_block,
    finish_document,
    location_span,
    plural,
    shorten,
    status_span,
    table,
    yes_no,
)
from leanc.reports.markdown.wording import MOCK_DISCLAIMER, STATUS_MEANING

COMMANDS_THAT_REPAIR = ("repair", "run")
WORKFLOW_STATUS = "Workflow status"


def _is_mock_run(rr: RunRecord) -> bool:
    return rr.provider is not None and not rr.provider.real_llm


def _artifacts_dir_of_round(rr: RunRecord, vr: VerificationResult) -> str | None:
    for r in rr.repair_rounds:
        if r.verification_path and vr.run_id.endswith(f"-r{r.round}"):
            return str(PurePosixPath(r.verification_path).parent) + "/"
    return None


def _policy_text(r: RepairRound) -> str:
    if r.policy is None:
        return "not checked"
    if r.policy.allowed:
        return "allowed"
    return f"**rejected** ({plural(len(r.policy.violations), 'violation')})"


def _transition(r: RepairRound) -> str:
    after = status_span(r.status_after) if r.status_after else "not re-verified"
    return f"{status_span(r.status_before)} → {after}"


def _ids(ids: list[str]) -> str:
    return ", ".join(code_span(i) for i in ids) if ids else "none"


def _rounds_table(rr: RunRecord) -> list[str]:
    return table(
        ["round", "provider", "status before → after", "+/- lines", "policy", "reverted", "note"],
        (
            [
                str(r.round),
                code_span(r.provider),
                _transition(r),
                f"+{r.lines_added} / -{r.lines_removed}",
                _policy_text(r),
                yes_no(r.reverted),
                escape_inline(r.note, LIMITS.note_cell_chars),
            ]
            for r in rr.repair_rounds
        ),
    )


def _policy_decision_lines(r: RepairRound) -> list[str]:
    if r.policy is None:
        return ["- Policy decision: not checked"]
    if r.policy.allowed:
        return ["- Policy decision: allowed"]
    out = [f"- Policy decision: **rejected**, {plural(len(r.policy.violations), 'violation')}:"]
    for v in r.policy.violations:
        where = ""
        if v.file:
            where = " at " + code_span(f"{v.file}:{v.line}" if v.line is not None else v.file)
        out.append(f"  - {code_span(v.rule)}{where}: {escape_inline(v.message, LIMITS.short_detail_chars)}")
    return out


def _round_details(r: RepairRound) -> list[str]:
    out = [f"### Round {r.round} (provider {code_span(r.provider)})", ""]
    out.append(f"- Status: {_transition(r)}")
    out.append(f"- Root cause (provider-supplied): {escape_inline(r.root_cause, LIMITS.detail_chars) or 'none given'}")
    files = ", ".join(code_span(f) for f in r.files_changed) or "none"
    out.append(f"- Files changed: {files} (+{r.lines_added} / -{r.lines_removed} lines)")
    out += _policy_decision_lines(r)
    out.append(f"- Fixed failures: {_ids(r.fixed_failures)}")
    out.append(f"- Introduced failures: {_ids(r.introduced_failures)}")
    out.append(f"- Reverted: {yes_no(r.reverted)}")
    out.append(f"- Patch: {code_span(r.patch_path) if r.patch_path else 'none'}")
    if r.verification_path:
        out.append(f"- Verification: {code_span(r.verification_path)}")
    if r.candidate_hash:
        out.append(f"- Candidate hash: {code_span(r.candidate_hash)}")
    if r.note:
        out.append(f"- Note: {escape_inline(r.note, LIMITS.detail_chars)}")
    out.append("")
    if r.rationale:
        out += ["Rationale (provider-supplied, unverified):", ""]
        out += fenced_block(shorten(r.rationale, LIMITS.rationale_chars))
        out.append("")
    return out


def _repair_history(rr: RunRecord) -> list[str]:
    out = ["## Repair history", ""]
    if _is_mock_run(rr):
        out += [f"> **{MOCK_DISCLAIMER}.** The proposals below were replayed from fixture files.", ""]
    if not rr.repair_rounds:
        return out + ["No repair round was run.", ""]
    rejected = sum(1 for r in rr.repair_rounds if r.policy is not None and not r.policy.allowed)
    reverted = sum(1 for r in rr.repair_rounds if r.reverted)
    out += [
        f"{plural(len(rr.repair_rounds), 'round')}; {rejected} rejected by policy; {reverted} reverted. "
        "Root causes and rationales are provider-supplied and unverified; only the re-verification "
        "status counts.",
        "",
    ]
    out += _rounds_table(rr)
    out.append("")
    for r in rr.repair_rounds:
        out += _round_details(r)
    return out


def _provider_section(rr: RunRecord) -> list[str]:
    out = ["## AI provider & generation", ""]
    p = rr.provider
    if p is not None:
        kind = "real LLM" if p.real_llm else f"not an LLM ({MOCK_DISCLAIMER})"
        out.append(f"- Provider: {code_span(p.name)}, model {code_span(p.model or 'none')}: {kind}")
    else:
        out.append("- Provider: none recorded")
    out.append(
        "- AI-generated and AI-repaired code is untrusted: it is judged only by the independent tools in the "
        "gates, never by the provider."
    )
    g = rr.generation
    if g is None:
        out += ["- Generation: no code was generated in this run", ""]
        return out
    generator = f" (provider {code_span(g.provider.name)}, real LLM: {yes_no(g.provider.real_llm)})"
    out.append(f"- Generation: {plural(len(g.files), 'file')} generated{generator}")
    if g.note:
        out.append(f"- Generation note: {escape_inline(g.note, LIMITS.detail_chars)}")
    out.append("")
    if g.files:
        out += table(
            ["generated file", "sha256 of generated content"],
            ([code_span(f), code_span(h)] for f, h in sorted(g.files.items())),
        )
        out.append("")
    return out


def _initial_failure_line(f: FailureReport) -> str:
    inputs = ", ".join(f"{i.name}={i.value}" for i in f.inputs)
    tail = f"; inputs {code_span(inputs, LIMITS.line_chars)}" if inputs else ""
    return (
        f"- {code_span(f.tool)} {escape_inline(f.kind)} at {location_span(f.location)}: "
        f"{escape_inline(f.description, LIMITS.short_detail_chars)} (id {code_span(f.id)}{tail})"
    )


def _initial_section(iv: VerificationResult) -> list[str]:
    out = ["## Initial verification", ""]
    out += [
        f"Before repair: {code_span(iv.status.value)} (run {code_span(iv.run_id)}). "
        f"{STATUS_MEANING.get(iv.status, '')}",
        "",
    ]
    if iv.stages:
        out += table(GATE_TABLE_HEADER, gate_rows(iv.stages))
        out.append("")
    failures = iv.all_failures()
    if failures:
        out += ["Failures found before repair:", ""]
        out += [_initial_failure_line(f) for f in failures[: LIMITS.findings_per_tool]]
        if len(failures) > LIMITS.findings_per_tool:
            out.append(f"- … {len(failures) - LIMITS.findings_per_tool} more")
        out.append("")
    return out


def _run_header(rr: RunRecord) -> list[str]:
    spec = code_span(rr.spec_id or "unknown")
    if rr.spec_path:
        spec += f" ({code_span(rr.spec_path)})"
    return [
        f"# Lean C run report: leanc {escape_inline(rr.command)}",
        "",
        f"- Run: {code_span(rr.run_id)}",
        f"- Command: {code_span('leanc ' + rr.command)}",
        f"- Spec: {spec}",
        f"- Project root: {code_span(rr.project_root)}",
        f"- Lean C version: {escape_inline(rr.leanc_version)}",
        f"- Started: {escape_inline(rr.started_at)}; finished: {escape_inline(rr.finished_at or 'not recorded')}",
        "",
    ]


def _mock_disclaimer(rr: RunRecord) -> list[str]:
    if not _is_mock_run(rr):
        return []
    return [
        f"> **Provider disclaimer: {MOCK_DISCLAIMER}.** Generated code and repair proposals in this run "
        "were replayed from fixtures; nothing here measures the performance of any LLM.",
        "",
    ]


def _has_distinct_initial_verification(rr: RunRecord) -> bool:
    iv, vr = rr.initial_verification, rr.verification
    return iv is not None and vr is not None and iv.run_id != vr.run_id


def _run_verdict(rr: RunRecord) -> list[str]:
    vr = rr.verification
    lines = ["## Verdict", ""]
    if vr is not None and vr.status is rr.status:
        lines += verdict_lines(rr.status, vr.reasons, label=WORKFLOW_STATUS)
        if rr.messages:
            lines += ["Workflow messages:", ""] + [f"- {escape_inline(m)}" for m in rr.messages] + [""]
    else:
        lines += verdict_lines(rr.status, rr.messages, label=WORKFLOW_STATUS)
        if vr is None:
            lines += ["**No verification was performed in this run: nothing has been verified.**", ""]
        else:
            lines += verdict_lines(vr.status, vr.reasons, label="Final verification status")
    if _has_distinct_initial_verification(rr):
        initial = rr.initial_verification.status.value
        lines += [f"Initial verification status: {code_span(initial)} → final: {code_span(vr.status.value)}", ""]
    return lines


def _final_verification_evidence(rr: RunRecord, vr: VerificationResult) -> list[str]:
    artifacts_dir = _artifacts_dir_of_round(rr, vr)
    where = f" (artifacts under {code_span(artifacts_dir)})" if artifacts_dir else ""
    lines = gates_section(vr, intro=f"Final verification {code_span(vr.run_id)}{where}.")
    lines += findings_section(vr)
    lines += counterexamples_section(vr)
    lines += coverage_section(vr)
    return lines


def _rerun_command(rr: RunRecord) -> str:
    argv = ["leanc", rr.command] + ([rr.spec_path] if rr.spec_path else [])
    if rr.provider is not None:
        argv += ["--provider", rr.provider.name]
    return shlex.join(argv)


def _artifacts_section(rr: RunRecord) -> list[str]:
    if not rr.artifacts:
        return []
    lines = ["## Artifacts", "", "Paths relative to the run directory.", ""]
    lines += table(["artifact", "path"], ([escape_inline(k), code_span(v)] for k, v in sorted(rr.artifacts.items())))
    lines.append("")
    return lines


def render_run_md(rr: RunRecord) -> str:
    vr = rr.verification
    lines = _run_header(rr)
    lines += _mock_disclaimer(rr)
    lines += _run_verdict(rr)
    if vr is not None:
        lines += reason_code_lines(vr.reason_codes)
        lines += scope_section(vr)
        lines += spec_section(vr)
        lines += environment_section(vr)
    if rr.provider is not None or rr.generation is not None:
        lines += _provider_section(rr)
    if vr is not None:
        lines += _final_verification_evidence(rr, vr)
    if _has_distinct_initial_verification(rr):
        lines += _initial_section(rr.initial_verification)
    if rr.repair_rounds or rr.command in COMMANDS_THAT_REPAIR:
        lines += _repair_history(rr)
    mock_note = f"{MOCK_DISCLAIMER}: repair and generation behaviour shown here is not evidence about any LLM."
    lines += limitations_section(vr, [mock_note] if _is_mock_run(rr) else [])
    lines += reproduce_section(vr, rerun=_rerun_command(rr))
    lines += _artifacts_section(rr)
    return finish_document(lines)
