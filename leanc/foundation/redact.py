from __future__ import annotations

import os
import re

SECRET_ENV_NAMES = (
    "LEANC_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "LEANC_APPROVAL_KEY",
)
MIN_SECRET_LENGTH = 8
_SECRET_NAME_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)

_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)['\"]?[A-Za-z0-9._\-]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
)

REDACTED = "[REDACTED]"
MAX_EXCERPT_CHARS = 300


def _is_secret_env_name(name: str) -> bool:
    return name in SECRET_ENV_NAMES or bool(_SECRET_NAME_RE.search(name))


def _longest_first_so_overlapping_secrets_vanish(values: set[str]) -> list[str]:
    return sorted(values, key=len, reverse=True)


def _secret_values() -> list[str]:
    values = {value for name, value in os.environ.items() if len(value) >= MIN_SECRET_LENGTH and _is_secret_env_name(name)}
    return _longest_first_so_overlapping_secrets_vanish(values)


def _mask_keeping_label(pattern: re.Pattern[str], text: str) -> str:
    if pattern.groups:
        return pattern.sub(lambda match: (match.group(1) or "") + REDACTED, text)
    return pattern.sub(REDACTED, text)


def redact(text: str, extra_secrets: list[str] | None = None) -> str:
    if not text:
        return text
    for value in (extra_secrets or []) + _secret_values():
        if value and len(value) >= MIN_SECRET_LENGTH:
            text = text.replace(value, REDACTED)
    for pattern in _PATTERNS:
        text = _mask_keeping_label(pattern, text)
    return text


def redacted_excerpt(text: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    return redact(text.strip())[:limit]
