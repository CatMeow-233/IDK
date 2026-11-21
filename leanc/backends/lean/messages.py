from __future__ import annotations

import json

from leanc.foundation.models import Diagnostic, Location, Severity

SEVERITY_BY_LEAN_SEVERITY: dict[str, Severity] = {"error": "error", "warning": "warning"}
OTHER_SEVERITY: Severity = "info"
ANONYMOUS_KIND = "[anonymous]"
MAX_MESSAGE_CHARS = 2000


def json_object(text: str) -> dict | None:
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    return value if isinstance(value, dict) else None


def _is_lean_message(candidate: dict | None) -> bool:
    return candidate is not None and "severity" in candidate and "data" in candidate


def parse_lean_messages(stdout: str) -> tuple[list[dict], list[str]]:
    messages: list[dict] = []
    other: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        candidate = json_object(stripped) if stripped.startswith("{") else None
        if _is_lean_message(candidate):
            messages.append(candidate)
        else:
            other.append(line)
    return messages, other


def errors_of(messages: list[dict]) -> list[dict]:
    return [message for message in messages if message.get("severity") == "error"]


def _one_based_column(pos: dict | None) -> int | None:
    if pos and isinstance(pos.get("column"), int):
        return int(pos["column"]) + 1
    return None


def message_location(message: dict) -> Location | None:
    pos = message.get("pos") if isinstance(message.get("pos"), dict) else None
    file = message.get("fileName")
    if not file and not pos:
        return None
    return Location(
        file=str(file) if file else None,
        line=int(pos["line"]) if pos and isinstance(pos.get("line"), int) else None,
        column=_one_based_column(pos),
    )


def message_diagnostic(message: dict) -> Diagnostic:
    kind = str(message.get("kind") or "")
    rule = kind if kind and kind != ANONYMOUS_KIND else f"lean-{message.get('severity', 'message')}"
    return Diagnostic(
        tool="lean",
        rule_id=rule,
        severity=SEVERITY_BY_LEAN_SEVERITY.get(str(message.get("severity")), OTHER_SEVERITY),
        message=str(message.get("data", "")).strip()[:MAX_MESSAGE_CHARS],
        location=message_location(message),
    )
