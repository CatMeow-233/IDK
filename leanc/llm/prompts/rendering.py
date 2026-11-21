from __future__ import annotations

import json

from leanc.foundation.models import (
    OWNERSHIP_TRACE_LABEL,
    Diagnostic,
    FailureReport,
    Location,
    VerificationResult,
)
from leanc.llm.prompts.text import (
    DEFAULT_LIMITS,
    PromptLimits,
    numbered_excerpt,
    truncate,
)
from leanc.specs.model import Spec

MAX_DIAGNOSTIC_PATH_STEPS = 10
MAX_TRACE_STEP_CHARS = 300


def _enum_text(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)


def render_location(loc: Location | None) -> str:
    return loc.short() if loc is not None else "?"


def render_diagnostic(d: Diagnostic, limits: PromptLimits = DEFAULT_LIMITS) -> str:
    rule = f" {d.rule_id}" if d.rule_id else ""
    where = f" at {render_location(d.location)}" if d.location else ""
    text = f"- [{d.tool}] {d.severity}{rule}{where}: {d.message}"
    for step in d.path[:MAX_DIAGNOSTIC_PATH_STEPS]:
        text += f"\n    path: {render_location(step.location)}: {step.message}"
    if len(d.path) > MAX_DIAGNOSTIC_PATH_STEPS:
        text += f"\n    path: ... {len(d.path) - MAX_DIAGNOSTIC_PATH_STEPS} more step(s)"
    return truncate(text, limits.max_item_chars, label="diagnostic")


def _inputs_line(f: FailureReport, limits: PromptLimits) -> str:
    shown = f.inputs[: limits.max_inputs]
    inputs = ", ".join(f"{i.name}={i.value}" for i in shown)
    if len(f.inputs) > len(shown):
        inputs += f", ... ({len(f.inputs) - len(shown)} more)"
    return f"  inputs: {truncate(inputs, limits.max_item_chars, label='inputs')}"


def _trace_lines(f: FailureReport, limits: PromptLimits) -> list[str]:
    steps = f.trace[-limits.max_trace_steps :]
    skipped = len(f.trace) - len(steps)
    lines = [f"  trace (last {len(steps)} step(s){f', {skipped} earlier omitted' if skipped else ''}):"]
    for step in steps:
        loc = f" {render_location(step.location)}" if step.location else ""
        lines.append(f"    [{step.kind}]{loc} {truncate(step.text, MAX_TRACE_STEP_CHARS, label='step')}".rstrip())
    return lines


def _code_excerpt_lines(f: FailureReport, sources: dict[str, str] | None) -> list[str]:
    if not (sources and f.location and f.location.file in sources and f.location.line):
        return []
    excerpt = numbered_excerpt(sources[f.location.file], f.location.line)
    if not excerpt:
        return []
    return [f"  code at {f.location.file}:{f.location.line}:", *("    " + x for x in excerpt.splitlines())]


def render_failure(
    f: FailureReport,
    limits: PromptLimits = DEFAULT_LIMITS,
    sources: dict[str, str] | None = None,
) -> str:
    lines = [f"- id: {f.id}", f"  kind: {f.kind}; detected by: {f.tool}"]
    if f.property_id:
        lines.append(f"  property: {f.property_id}")
    lines.append(f"  description: {truncate(f.description, limits.max_item_chars, label='description')}")
    if f.location:
        lines.append(f"  location: {render_location(f.location)}")
    if f.inputs:
        lines.append(_inputs_line(f, limits))
    if f.call_stack:
        lines.append(f"  call stack: {' -> '.join(f.call_stack)}")
    if f.trace:
        lines.extend(_trace_lines(f, limits))
    if f.explanation:
        lines.append(f"  explanation: {truncate(f.explanation, limits.max_item_chars, label='explanation')}")
    if f.ownership_trace is not None:
        evidence = truncate(f.ownership_trace.describe(), limits.max_item_chars, label="ownership trace")
        lines.append(f"  {OWNERSHIP_TRACE_LABEL}: {evidence}")
    lines.extend(_code_excerpt_lines(f, sources))
    return "\n".join(lines)


def render_failures(
    failures: list[FailureReport], limits: PromptLimits = DEFAULT_LIMITS, sources: dict[str, str] | None = None
) -> str:
    if not failures:
        return "(none)"
    shown = failures[: limits.max_failures]
    out = [render_failure(f, limits, sources) for f in shown]
    if len(failures) > len(shown):
        out.append(f"- ... {len(failures) - len(shown)} more failure(s) omitted")
    return "\n".join(out)


def render_diagnostics(diags: list[Diagnostic], limits: PromptLimits = DEFAULT_LIMITS) -> str:
    if not diags:
        return "(none)"
    shown = diags[: limits.max_diagnostics]
    out = [render_diagnostic(d, limits) for d in shown]
    if len(diags) > len(shown):
        out.append(f"- ... {len(diags) - len(shown)} more diagnostic(s) omitted")
    return "\n".join(out)


def render_history(history: list[dict], limits: PromptLimits = DEFAULT_LIMITS) -> str:
    if not history:
        return "(this is the first round)"
    shown = history[-limits.max_history :]
    out = []
    if len(history) > len(shown):
        out.append(f"- ... {len(history) - len(shown)} earlier round(s) omitted")
    for entry in shown:
        entry_json = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
        out.append("- " + truncate(entry_json, limits.max_item_chars, label="round"))
    return "\n".join(out)


def render_verification(result: VerificationResult, limits: PromptLimits = DEFAULT_LIMITS) -> str:
    head = f"status: {_enum_text(result.status)}"
    if result.spec_id:
        head += f"\nspec: {result.spec_id}" + (f" v{result.spec_version}" if result.spec_version else "")
    lines = [head]
    if result.reasons:
        lines.append("reasons:")
        lines.extend(f"- {truncate(r, limits.max_item_chars, label='reason')}" for r in result.reasons)
    if result.stages:
        lines.append("stages:")
        for stage in result.stages:
            optional = "" if stage.required else " (optional)"
            summary = f": {truncate(stage.summary, limits.max_item_chars, label='summary')}" if stage.summary else ""
            lines.append(f"- {stage.stage}{optional}: {_enum_text(stage.outcome)}{summary}")
    failures = result.all_failures()
    lines.append(f"failures ({len(failures)}):")
    lines.append(render_failures(failures, limits))
    if result.assumptions:
        lines.append("assumptions (scope of a bounded result):")
        lines.extend(f"- {a}" for a in result.assumptions)
    if result.limitations:
        lines.append("limitations:")
        lines.extend(f"- {x}" for x in result.limitations)
    return "\n".join(lines)


def interface_summary(spec: Spec) -> str:
    target = spec.target
    lines = [f"function: {target.function}", f"signature: {target.signature}"]
    for p in target.params:
        lines.append(f"param {p.name}: {p.type}" + (f" - {p.description}" if p.description else ""))
    returns = target.returns
    lines.append(f"returns: {returns.type}" + (f" - {returns.description}" if returns.description else ""))
    for v in returns.values:
        lines.append(f"  {v.value}: {v.meaning}")
    contract = spec.contract
    for label, items in (("pre", contract.preconditions), ("post", contract.postconditions)):
        for cond in items:
            lines.append(f"{label} {cond.id}: {cond.text}")
    for s in contract.safety:
        lines.append(f"safety {s.id}: {s.text}")
    for frame in contract.frame:
        lines.append(f"frame: {frame}")
    return "\n".join(lines)
