from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

PROPERTY_VIOLATED = "FAILURE"
PROPERTY_HOLDS = "SUCCESS"
JSON_MESSAGE_SEPARATORS = " \t\r\n,"
PROGRAM_NAME_PREFIX = "CBMC "

_VERSION_LINE_RE = re.compile(r"^CBMC version (?P<ver>\S+(?: \([^)]*\))?)\s+(?P<plat>.+)$")


def _skip_separators(text: str, position: int) -> int:
    while position < len(text) and text[position] in JSON_MESSAGE_SEPARATORS:
        position += 1
    return position


def _salvage_complete_messages(text: str) -> list[dict]:
    array_start = text.find("[")
    if array_start < 0:
        return []
    decoder = json.JSONDecoder()
    messages: list[dict] = []
    position = array_start + 1
    while True:
        position = _skip_separators(text, position)
        if position >= len(text) or text[position] == "]":
            return messages
        try:
            message, position = decoder.raw_decode(text, position)
        except ValueError:
            return messages
        if isinstance(message, dict):
            messages.append(message)


def parse_cbmc_json(text: str) -> tuple[list[dict], str | None]:
    stripped = (text or "").strip()
    if not stripped:
        return [], "empty output"
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return _salvage_complete_messages(stripped), f"invalid JSON ({exc.msg} at line {exc.lineno} column {exc.colno})"
    if isinstance(data, dict):
        return [data], "unexpected JSON: top level is an object, not an array"
    if not isinstance(data, list):
        return [], f"unexpected JSON: top level is {type(data).__name__}"
    return [message for message in data if isinstance(message, dict)], None


@dataclass
class CbmcOutput:
    messages: list[dict] = field(default_factory=list)
    parse_error: str | None = None
    results: list[dict] | None = None
    goals: list[dict] | None = None
    cprover_status: str | None = None
    program: str | None = None
    platform: str | None = None
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)

    def error_texts(self) -> list[str]:
        return [str(m.get("messageText", "")).strip() for m in self.errors if str(m.get("messageText", "")).strip()]


def _record_text_message(output: CbmcOutput, message: dict) -> None:
    message_type = message.get("messageType")
    if message_type == "ERROR":
        output.errors.append(message)
    elif message_type == "WARNING":
        output.warnings.append(message)
    elif output.platform is None:
        version_line = _VERSION_LINE_RE.match(str(message["messageText"]).strip())
        if version_line:
            output.platform = version_line.group("plat")


def interpret_output(text: str) -> CbmcOutput:
    messages, error = parse_cbmc_json(text)
    output = CbmcOutput(messages=messages, parse_error=error)
    for message in messages:
        if "result" in message and isinstance(message["result"], list):
            output.results = [result for result in message["result"] if isinstance(result, dict)]
        elif "goals" in message and isinstance(message["goals"], list):
            output.goals = [goal for goal in message["goals"] if isinstance(goal, dict)]
        elif "cProverStatus" in message:
            output.cprover_status = str(message["cProverStatus"])
        elif "program" in message:
            output.program = str(message["program"])
        elif "messageText" in message:
            _record_text_message(output, message)
    return output


def program_version(program: str | None) -> str | None:
    if not program:
        return None
    return program.removeprefix(PROGRAM_NAME_PREFIX).strip() if program.startswith(PROGRAM_NAME_PREFIX) else program
