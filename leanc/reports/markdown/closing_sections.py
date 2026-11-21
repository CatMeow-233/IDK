from __future__ import annotations

import shlex
from typing import Iterable

from leanc.foundation.models import ToolInvocation, VerificationResult
from leanc.reports.markdown.markup import LIMITS, code_span, duration, escape_inline, fenced_block
from leanc.reports.markdown.wording import ALWAYS_LIMITATIONS, restates_always_limitation


def limitations_section(vr: VerificationResult | None, extra: Iterable[str] = ()) -> list[str]:
    out = ["## Limitations", ""]
    items: list[str] = list(ALWAYS_LIMITATIONS)
    for item in [*extra, *(vr.limitations if vr else [])]:
        if item in items or restates_always_limitation(item):
            continue
        items.append(item)
    out += [f"- {escape_inline(i)}" for i in items]
    out.append("")
    return out


def _invocation_facts(inv: ToolInvocation) -> list[str]:
    facts = [f"exit {inv.exit_code if inv.exit_code is not None else '-'}"]
    if inv.signal:
        facts.append(f"signal {inv.signal}")
    if inv.timed_out:
        facts.append("**timed out**")
    if inv.output_truncated:
        facts.append("output truncated")
    facts += [duration(inv.duration_s), f"sandbox {code_span(inv.sandbox)}"]
    if inv.stdout_path:
        facts.append(f"stdout {code_span(inv.stdout_path)}")
    if inv.stderr_path:
        facts.append(f"stderr {code_span(inv.stderr_path)}")
    return facts


def _command_line(inv: ToolInvocation) -> str:
    cmd = shlex.join(inv.argv) if inv.argv else "(argv not recorded)"
    return f"cd {shlex.quote(inv.cwd)} && {cmd}" if inv.cwd else cmd


def _invocation(stage: str, k: int, inv: ToolInvocation) -> list[str]:
    out = [f"{code_span(stage)} #{k}, {code_span(inv.tool)}: {'; '.join(_invocation_facts(inv))}", ""]
    if inv.note:
        out += [f"Note: {escape_inline(inv.note, LIMITS.detail_chars)}", ""]
    out += fenced_block(_command_line(inv), "sh")
    out.append("")
    return out


def reproduce_section(vr: VerificationResult | None, rerun: str | None = None) -> list[str]:
    out = ["## Reproduce", ""]
    if rerun:
        out += ["Re-run the workflow (flags other than the provider were not recorded):", ""]
        out += fenced_block(rerun, "sh")
        out.append("")
    invocations = [(s.stage, k, inv) for s in (vr.stages if vr else []) for k, inv in enumerate(s.invocations, 1)]
    if not invocations:
        return out + ["No tool invocation was recorded.", ""]
    out += [
        "Tool invocations in execution order (argv as run, before any sandbox wrapping; secrets redacted; "
        "log paths are relative to the verification directory):",
        "",
    ]
    for stage, k, inv in invocations:
        out += _invocation(stage, k, inv)
    return out
