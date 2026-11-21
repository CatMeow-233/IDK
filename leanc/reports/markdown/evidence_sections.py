"""Report sections of tool evidence: gates, findings grouped by detecting tool, counterexamples and coverage."""

from __future__ import annotations

from enum import Enum
from typing import Iterable, NamedTuple

from leanc.foundation.models import Diagnostic, FailureReport, Location, StageResult, TraceStep, VerificationResult
from leanc.reports.json_report import detecting_tools, is_finding, sort_tools
from leanc.reports.markdown.markup import (
    LIMITS,
    code_span,
    duration,
    escape_inline,
    fenced_block,
    flatten,
    location_span,
    plural,
    shorten,
    status_span,
    table,
    table_cell,
    tool_heading,
)

GATE_TABLE_HEADER = ["stage", "outcome", "status", "detecting tools", "duration", "summary"]
DYNAMIC_FAILURE_KINDS = ("runtime", "test-oracle")


class FindingKind(Enum):
    FAILURE = "failure"
    DIAGNOSTIC = "diagnostic"


class Finding(NamedTuple):
    kind: FindingKind
    stage: StageResult
    item: FailureReport | Diagnostic


def gate_rows(stages: list[StageResult]) -> Iterable[list[str]]:
    for s in stages:
        name = code_span(s.stage) + ("" if s.required else " (optional)")
        outcome = s.outcome.value if s.outcome.value == "PASS" else f"**{s.outcome.value}**"
        tools = ", ".join(code_span(t) for t in detecting_tools(s)) or "-"
        yield [name, outcome, status_span(s.status), tools, duration(s.duration_s), escape_inline(s.summary, LIMITS.cell_chars)]


def gates_section(vr: VerificationResult, intro: str | None = None) -> list[str]:
    out = ["## Gates", ""]
    if intro:
        out += [intro, ""]
    if not vr.stages:
        return out + ["No gate was run.", ""]
    out += table(GATE_TABLE_HEADER, gate_rows(vr.stages))
    out += ["", "*detecting tools* = tools that reported a failure or an error/warning diagnostic in that gate.", ""]
    return out


def _finding_key(tool: str, loc: Location | None, message: str) -> tuple:
    loc = loc or Location()
    return (tool, loc.file, loc.line, loc.column, message)


def _group_findings(vr: VerificationResult) -> tuple[dict[str, list[Finding]], int]:
    groups: dict[str, list[Finding]] = {}
    note_level = 0
    for s in vr.stages:
        reported_as_failure = {_finding_key(f.tool, f.location, f.description) for f in s.failures}
        for f in s.failures:
            groups.setdefault(f.tool, []).append(Finding(FindingKind.FAILURE, s, f))
        for d in s.diagnostics:
            if not is_finding(d):
                note_level += 1
                continue
            if _finding_key(d.tool, d.location, d.message) in reported_as_failure:
                continue
            groups.setdefault(d.tool, []).append(Finding(FindingKind.DIAGNOSTIC, s, d))
    return groups, note_level


def _failure_lines(stage: StageResult, f: FailureReport) -> list[str]:
    meta = [f"stage {code_span(stage.stage)}"]
    if f.property_id:
        meta.append(f"property {code_span(f.property_id)}")
    meta.append(f"id {code_span(f.id)}")
    if f.raw_ref:
        meta.append(f"raw output {code_span(f.raw_ref)}")
    return [
        f"- **{escape_inline(f.kind)}** at {location_span(f.location)}: {escape_inline(f.description, LIMITS.detail_chars)}",
        "  - " + "; ".join(meta),
    ]


def _diagnostic_lines(stage: StageResult, d: Diagnostic) -> list[str]:
    meta = [f"stage {code_span(stage.stage)}"]
    if d.rule_id:
        meta.append(f"rule {code_span(d.rule_id)}")
    return [
        f"- {escape_inline(d.severity)} (non-blocking) at {location_span(d.location)}: "
        f"{escape_inline(d.message, LIMITS.detail_chars)}",
        "  - " + "; ".join(meta),
    ]


def _tool_findings(tool: str, items: list[Finding]) -> list[str]:
    failures = sum(1 for finding in items if finding.kind is FindingKind.FAILURE)
    diagnostics = len(items) - failures
    counts = [plural(failures, "failure")] + ([plural(diagnostics, "non-blocking diagnostic")] if diagnostics else [])
    out = [f"### {tool_heading(tool)}: {', '.join(counts)}", ""]
    for finding in items[: LIMITS.findings_per_tool]:
        if isinstance(finding.item, FailureReport):
            out += _failure_lines(finding.stage, finding.item)
        else:
            out += _diagnostic_lines(finding.stage, finding.item)
    if len(items) > LIMITS.findings_per_tool:
        out.append(f"- … {len(items) - LIMITS.findings_per_tool} more (see verification.json)")
    out.append("")
    return out


def findings_section(vr: VerificationResult) -> list[str]:
    out = ["## Findings by detecting tool", ""]
    groups, note_level = _group_findings(vr)
    if not groups:
        out += [
            "No tool reported a failure or warning. This alone does not mean the code is correct: "
            "see the Verdict and the Gates table for what was actually established.",
            "",
        ]
    for tool in sort_tools(groups):
        out += _tool_findings(tool, groups[tool])
    if note_level:
        out += [
            f"{plural(note_level, 'note-level diagnostic')} (context for other findings) omitted; see verification.json.",
            "",
        ]
    return out


def _is_counterexample(f: FailureReport) -> bool:
    return f.kind == "counterexample" or bool(f.inputs) or bool(f.call_stack) or (
        f.kind in DYNAMIC_FAILURE_KINDS and bool(f.trace)
    )


def _trace_line(idx: int, step: TraceStep) -> str:
    where = f" {step.location.short()}" if step.location else ""
    return shorten(f"#{idx} {step.kind}{where}: {flatten(step.text)}", LIMITS.line_chars)


def _trace_excerpt(f: FailureReport) -> list[str]:
    total = len(f.trace)
    start = max(0, total - LIMITS.trace_excerpt_steps)
    shown = f"last {total - start} of {total} steps" if start else f"all {total} steps"
    out = [f"Trace excerpt ({shown}):", ""]
    out += fenced_block("\n".join(_trace_line(i + 1, f.trace[i]) for i in range(start, total)))
    out.append("")
    return out


def _counterexample(n: int, stage: StageResult, f: FailureReport) -> list[str]:
    title = code_span(f.property_id) if f.property_id else escape_inline(f.description, LIMITS.title_chars)
    out = [f"### {n}. {title} ({code_span(f.tool)})", ""]
    out.append(
        f"- Property: {code_span(f.property_id) if f.property_id else 'not named'}: "
        f"{escape_inline(f.description, LIMITS.detail_chars)}"
    )
    out.append(f"- Location: {location_span(f.location)}")
    out.append(f"- Detected by {code_span(f.tool)} in stage {code_span(stage.stage)}; failure id {code_span(f.id)}")
    out.append("")
    if f.inputs:
        out += ["Inputs:", ""]
        out += table(
            ["name", "value", "type"],
            ([code_span(i.name), code_span(i.value), code_span(i.type) if i.type else "-"] for i in f.inputs),
        )
        out.append("")
    else:
        out += ["Inputs: none recorded.", ""]
    if f.call_stack:
        out += ["Call stack (outermost first):", ""]
        out += [f"{k}. {code_span(frame, LIMITS.line_chars)}" for k, frame in enumerate(f.call_stack, 1)]
        out.append("")
    if f.trace:
        out += _trace_excerpt(f)
    if f.explanation:
        out += [f"Explanation: {escape_inline(f.explanation, LIMITS.explanation_chars)}", ""]
    out += [f"Raw tool output: {code_span(f.raw_ref) if f.raw_ref else 'not recorded'}", ""]
    return out


def counterexamples_section(vr: VerificationResult) -> list[str]:
    out = ["## Counterexamples", ""]
    items = [(s, f) for s in vr.stages for f in s.failures if _is_counterexample(f)]
    if not items:
        return out + ["No counterexample or failing execution was recorded.", ""]
    out += [
        "Concrete evidence: CBMC counterexamples and failing sanitizer / test-oracle executions "
        "(paths are relative to the verification directory).",
        "",
    ]
    for n, (s, f) in enumerate(items[: LIMITS.counterexamples], 1):
        out += _counterexample(n, s, f)
    if len(items) > LIMITS.counterexamples:
        out += [f"… {len(items) - LIMITS.counterexamples} more (see verification.json).", ""]
    return out


def _coverage_row(file: str, entry: object) -> str:
    counts = entry if isinstance(entry, dict) else {}
    uncovered = counts.get("uncovered_lines") or []
    limit = LIMITS.uncovered_lines_shown
    shown = ", ".join(str(x) for x in uncovered[:limit]) + (" ..." if len(uncovered) > limit else "")
    cells = [code_span(file), str(counts.get("goals", "?")), str(counts.get("covered", "?")), shown or "-"]
    return "| " + " | ".join(table_cell(c) for c in cells) + " |"


def coverage_section(vr: VerificationResult | None) -> list[str]:
    """CBMC ``--cover location`` results, present only when the spec enables ``verification.coverage``."""
    if vr is None:
        return []
    stage = vr.stage("cbmc")
    coverage = stage.details.get("coverage") if stage is not None else None
    if not isinstance(coverage, dict) or not coverage:
        return []
    out = [
        "## Coverage under the harness assumptions",
        "",
        "Location coverage of the editable sources reachable under the approved harness (CBMC `--cover location`). "
        "Uncovered lines can indicate over-constrained assumptions or dead code; coverage is advisory and never "
        "changes the verdict.",
        "",
        "| file | goals | covered | uncovered lines |",
        "|---|---|---|---|",
    ]
    out += [_coverage_row(file, coverage[file]) for file in sorted(coverage)]
    return out + [""]
