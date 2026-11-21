"""Report sections that frame a verdict: the verdict itself, its scope, the spec and approval, the environment."""

from __future__ import annotations

from leanc.foundation.models import ApprovalCheck, VerificationResult
from leanc.foundation.status import Status, exit_code, is_success
from leanc.reports.markdown.markup import code_span, escape_inline, table, yes_no
from leanc.reports.markdown.wording import STATUS_MEANING

NO_STATUS_DESCRIPTION = "no description available for this status"
UNTRUSTED_SPEC_WARNING = "**No result in this report is a verification result against a trusted specification.**"
PROVED_IS_CONTRACT_RELATIVE = (
    "**PROVED is relative to the approved ACSL contract: review that the contract states what you need.**"
)


def verdict_lines(status: Status, reasons: list[str], *, label: str = "Status") -> list[str]:
    out = [f"**{label}: {code_span(status.value)}** (exit code {exit_code(status)})", ""]
    out += [f"Meaning: {STATUS_MEANING.get(status, NO_STATUS_DESCRIPTION)}", ""]
    if status is Status.SPEC_UNAPPROVED:
        out += [UNTRUSTED_SPEC_WARNING, ""]
    if status is Status.PROVED:
        out += [PROVED_IS_CONTRACT_RELATIVE, ""]
    if reasons:
        out.append("Reasons:")
        out.append("")
        out += [f"- {escape_inline(r)}" for r in reasons]
    else:
        out.append("Reasons: none recorded.")
    out.append("")
    return out


def _hash_table(hashes: dict[str, str]) -> list[str]:
    return table(["file", "sha256"], ([code_span(p), code_span(h)] for p, h in sorted(hashes.items())))


def scope_section(vr: VerificationResult) -> list[str]:
    out = ["## Scope & assumptions", ""]
    if is_success(vr.status):
        out += [f"The {code_span(vr.status.value)} verdict holds **only** within this scope:", ""]
    else:
        out += ["Conditions under which the gates were run (no success is claimed):", ""]
    if vr.assumptions:
        out += [f"- {escape_inline(a)}" for a in vr.assumptions]
    else:
        out.append("- none recorded")
    out.append("")
    if vr.source_hashes:
        out += ["Sources examined:", ""]
        out += _hash_table(vr.source_hashes)
        out.append("")
    return out


def _approval_lines(a: ApprovalCheck | None) -> list[str]:
    if a is None:
        return ["- Approval: **no approval check recorded**: treat the specification as untrusted"]
    if a.approved:
        return [
            f"- Approval: **approved** by {code_span(a.approved_by or 'unknown')} at "
            f"{escape_inline(a.approved_at or 'unknown time')}; signature valid: {yes_no(a.signature_valid)}"
        ]
    out = [
        "- Approval: **NOT approved**: nothing in this report was formally verified against a trusted "
        f"specification (signature valid: {yes_no(a.signature_valid)})"
    ]
    out += [f"  - {escape_inline(r)}" for r in a.reasons] or ["  - no reason recorded"]
    return out


def spec_section(vr: VerificationResult) -> list[str]:
    out = ["## Specification & approval", ""]
    a = vr.approval
    spec_id = vr.spec_id or (a.spec_id if a else None)
    version = vr.spec_version if vr.spec_version is not None else (a.spec_version if a else None)
    spec_hash = vr.spec_hash or (a.spec_hash if a else None)
    out.append(
        f"- Spec id: {code_span(spec_id or 'unknown')}, version "
        f"{escape_inline(version if version is not None else 'unknown')}"
    )
    out.append(f"- Spec hash (sha256): {code_span(spec_hash or 'unknown')}")
    out += _approval_lines(a)
    out.append("")
    if a and a.protected_files:
        out += ["Protected (trusted) files, hashes as approved:", ""]
        out += _hash_table(a.protected_files)
        out.append("")
    return out


def environment_section(vr: VerificationResult) -> list[str]:
    out = ["## Environment", ""]
    env = vr.environment
    if env is None:
        return out + ["Environment information was not recorded.", ""]
    out.append(f"- Lean C {escape_inline(env.leanc_version)}; Python {escape_inline(env.python)}; {escape_inline(env.platform)}")
    note = f": {escape_inline(env.sandbox_note)}" if env.sandbox_note else ""
    out.append(f"- Sandbox: {code_span(env.sandbox)}{note}")
    if env.sandbox == "none":
        out.append("- **No sandbox was active**: untrusted programs were not isolated (or were not executed).")
    out.append("")
    if env.tools:
        out += table(
            ["tool", "available", "version", "path", "note"],
            (
                [
                    code_span(t.name),
                    yes_no(t.available),
                    escape_inline(t.version) if t.version else "-",
                    code_span(t.path or "-"),
                    escape_inline(t.note or ""),
                ]
                for t in env.tools
            ),
        )
        out.append("")
    return out
