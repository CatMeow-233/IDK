"""Credential redaction for logs, reports and prompts."""

from __future__ import annotations

import os
import re

# Environment variables whose values must never appear in logs.
SECRET_ENV_NAMES = (
    "LEANC_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "LEANC_APPROVAL_KEY",
)
_SECRET_NAME_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)

_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)['\"]?[A-Za-z0-9._\-]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
)

REDACTED = "[REDACTED]"


def _secret_values() -> list[str]:
    vals = []
    for name, value in os.environ.items():
        if not value or len(value) < 8:
            continue
        if name in SECRET_ENV_NAMES or _SECRET_NAME_RE.search(name):
            vals.append(value)
    # longest first so overlapping secrets are fully removed
    return sorted(set(vals), key=len, reverse=True)


def redact(text: str, extra_secrets: list[str] | None = None) -> str:
    if not text:
        return text
    for value in (extra_secrets or []) + _secret_values():
        if value and len(value) >= 8:
            text = text.replace(value, REDACTED)
    for pat in _PATTERNS:
        if pat.groups:
            text = pat.sub(lambda m: (m.group(1) or "") + REDACTED, text)
        else:
            text = pat.sub(REDACTED, text)
    return text
