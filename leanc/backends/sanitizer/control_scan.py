from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from leanc.foundation.models import Diagnostic, Location

POLICY_TOOL = "leanc-policy"
SANITIZER_CONTROL_RULE = "sanitizer-control"
RAW_LINE_MAX_CHARS = 300

_SANITIZER_CONTROL_RE = re.compile(
    r"\b__(?:asan|lsan|ubsan|msan|tsan|hwasan|dfsan|sanitizer|sancov)_\w+"
    r"|\bno_sanitize\w*"
    r"|\bdisable_sanitizer_instrumentation\b"
    r"|\b__SANITIZE_\w+__"
    r"|\b__has_feature\s*\(\s*\w*sanitizer\w*\s*\)"
    r"|#\s*include\s*[<\"]\s*sanitizer/"
)


def _find_or_end(source: str, needle: str, start: int) -> int:
    position = source.find(needle, start)
    return len(source) if position < 0 else position


def _literal_end(source: str, start: int) -> int:
    quote = source[start]
    position = start + 1
    while position < len(source) and source[position] != quote and source[position] != "\n":
        position += 2 if source[position] == "\\" else 1
    return min(position + 1, len(source))


def _blank_keeping_newlines(text: str) -> str:
    return "".join(ch if ch == "\n" else " " for ch in text)


def _next_lexeme(source: str, start: int) -> tuple[int, str]:
    if source.startswith("//", start):
        end = _find_or_end(source, "\n", start)
        return end, " " * (end - start)
    if source.startswith("/*", start):
        close = source.find("*/", start + 2)
        end = len(source) if close < 0 else close + 2
        return end, _blank_keeping_newlines(source[start:end])
    if source[start] in "\"'":
        end = _literal_end(source, start)
        return end, source[start:end]
    return start + 1, source[start]


def _blank_out_comments_keeping_literals(source: str) -> str:
    pieces: list[str] = []
    position = 0
    while position < len(source):
        position, piece = _next_lexeme(source, position)
        pieces.append(piece)
    return "".join(pieces)


def _control_diagnostic(rel: str, line_number: int, line: str, match: re.Match) -> Diagnostic:
    return Diagnostic(
        tool=POLICY_TOOL,
        rule_id=SANITIZER_CONTROL_RULE,
        severity="error",
        message=(
            f"editable source uses {match.group(0).strip()!r}: code under test must not configure, "
            "suppress or detect the sanitizers that judge it"
        ),
        location=Location(file=rel, line=line_number, column=match.start() + 1),
        raw=line.strip()[:RAW_LINE_MAX_CHARS],
    )


def scan_sanitizer_control(snapshot: Path, editable: Iterable[str]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for rel in editable:
        path = snapshot / rel
        if not path.is_file():
            continue
        code = _blank_out_comments_keeping_literals(path.read_text(encoding="utf-8", errors="replace"))
        for line_number, line in enumerate(code.splitlines(), 1):
            diagnostics.extend(
                _control_diagnostic(rel, line_number, line, match) for match in _SANITIZER_CONTROL_RE.finditer(line)
            )
    return diagnostics
