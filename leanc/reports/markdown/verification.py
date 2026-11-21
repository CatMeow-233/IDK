from __future__ import annotations

from leanc.foundation.models import VerificationResult
from leanc.reports.markdown.closing_sections import limitations_section, reproduce_section
from leanc.reports.markdown.context_sections import environment_section, scope_section, spec_section, verdict_lines
from leanc.reports.markdown.evidence_sections import (
    counterexamples_section,
    coverage_section,
    findings_section,
    gates_section,
)
from leanc.reports.markdown.markup import code_span, escape_inline, finish_document


def _verification_header(vr: VerificationResult) -> list[str]:
    spec = f"{vr.spec_id or 'unknown spec'}" + (f" v{vr.spec_version}" if vr.spec_version is not None else "")
    return [
        f"# Lean C verification report: {escape_inline(spec)}",
        "",
        f"- Run: {code_span(vr.run_id)}",
        f"- Started: {escape_inline(vr.started_at)}; finished: {escape_inline(vr.finished_at or 'not recorded')}",
        "",
    ]


def render_verification_md(vr: VerificationResult) -> str:
    lines = _verification_header(vr)
    lines += ["## Verdict", ""] + verdict_lines(vr.status, vr.reasons)
    lines += scope_section(vr)
    lines += spec_section(vr)
    lines += environment_section(vr)
    lines += gates_section(vr)
    lines += findings_section(vr)
    lines += counterexamples_section(vr)
    lines += coverage_section(vr)
    lines += limitations_section(vr)
    lines += reproduce_section(vr)
    return finish_document(lines)
