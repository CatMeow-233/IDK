"""Markdown primitives that render untrusted tool and provider text inertly: escaping, code spans, fences, tables."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from leanc.foundation.models import Location
from leanc.foundation.redact import redact
from leanc.foundation.status import Status
from leanc.reports.markdown.wording import TOOL_LABELS


@dataclass(frozen=True)
class ReportLimits:
    findings_per_tool: int = 50
    counterexamples: int = 20
    trace_excerpt_steps: int = 15
    uncovered_lines_shown: int = 40
    cell_chars: int = 300
    line_chars: int = 200
    title_chars: int = 80
    note_cell_chars: int = 160
    short_detail_chars: int = 300
    detail_chars: int = 500
    explanation_chars: int = 1000
    rationale_chars: int = 2000


LIMITS = ReportLimits()
MIN_FENCE_TICKS = 3
ELLIPSIS = "…"

_MARKUP_CHARACTER = r"[\\`*]"
_HTML_TAG_OR_AUTOLINK_OPENER = r"<(?=[A-Za-z/!?])"
_UNDERSCORE_AT_WORD_BOUNDARY = r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])"
_INLINE_MARKUP_RE = re.compile("|".join((_MARKUP_CHARACTER, _HTML_TAG_OR_AUTOLINK_OPENER, _UNDERSCORE_AT_WORD_BOUNDARY)))
_LINK_TARGET_OPENER = "]("
_BLOCK_MARKERS_AT_LINE_START = {
    "atx_heading": re.compile(r"^(#{1,6})(?=\s|$)"),
    "block_quote": re.compile(r"^(>)"),
    "bullet": re.compile(r"^([-+])(?=\s|$)"),
    "setext_underline_or_thematic_break": re.compile(r"^(=+|-+)(?=\s*$)"),
    "tilde_fence": re.compile(r"^(~~~)"),
}
_ORDERED_LIST_MARKER_RE = re.compile(r"^(\d{1,9})([.)])(?=\s|$)")
_NEWLINES_RE = re.compile(r"\s*[\r\n]+\s*")
_BACKTICK_RUN_RE = re.compile(r"`+")
_FENCE_OPEN_RE = re.compile(r"^(`{3,})[^`]*$")


def flatten(text: object) -> str:
    return _NEWLINES_RE.sub(" ", "" if text is None else str(text)).strip()


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + ELLIPSIS


def escape_inline(text: object, limit: int | None = None) -> str:
    """Escape untrusted text for inline use (single line, no markup)."""
    s = flatten(text)
    if limit:
        s = shorten(s, limit)
    s = _INLINE_MARKUP_RE.sub(lambda m: "\\" + m.group(0), s)
    s = s.replace(_LINK_TARGET_OPENER, "\\" + _LINK_TARGET_OPENER)
    for marker in _BLOCK_MARKERS_AT_LINE_START.values():
        s = marker.sub(r"\\\1", s)
    s = _ORDERED_LIST_MARKER_RE.sub(r"\1\\\2", s)
    return s


def _longest_backtick_run(text: str) -> int:
    return max((len(run) for run in _BACKTICK_RUN_RE.findall(text)), default=0)


def _needs_padding_inside_code_span(s: str) -> bool:
    return s[0] == "`" or s[-1] == "`" or (s[0] == " " and s[-1] == " ")


def code_span(text: object, limit: int | None = None) -> str:
    """Inline code span that is safe for any content (backticks included)."""
    s = flatten(text)
    if limit:
        s = shorten(s, limit)
    if not s:
        return "-"
    fence = "`" * (_longest_backtick_run(s) + 1)
    pad = " " if _needs_padding_inside_code_span(s) else ""
    return f"{fence}{pad}{s}{pad}{fence}"


def _unix_newlines(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def table_cell(text: str) -> str:
    """Make already-escaped inline markdown safe inside a GFM table cell."""
    s = _unix_newlines(text).strip()
    s = s.replace("|", "\\|").replace("\n", "<br>")
    return s or " "


def fenced_block(text: str, lang: str = "text") -> list[str]:
    """Fenced block whose fence cannot be closed by the content."""
    body = _unix_newlines(text).rstrip("\n")
    fence = "`" * max(MIN_FENCE_TICKS, _longest_backtick_run(body) + 1)
    return [f"{fence}{lang}", *body.split("\n"), fence]


def table(header: list[str], rows: Iterable[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for row in rows:
        out.append("| " + " | ".join(table_cell(c) for c in row) + " |")
    return out


def yes_no(flag: bool | None) -> str:
    return "unknown" if flag is None else ("yes" if flag else "no")


def duration(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds:.2f} s"


def location_span(loc: Location | None) -> str:
    return code_span(loc.short()) if loc else "unknown location"


def status_span(status: Status | None) -> str:
    return code_span(status.value) if status else "-"


def tool_heading(tool: str) -> str:
    label = TOOL_LABELS.get(tool)
    return f"{code_span(tool)} ({label})" if label else code_span(tool)


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _collapse_blank_runs_outside_fences(lines: list[str]) -> list[str]:
    out: list[str] = []
    open_fence: str | None = None
    for line in lines:
        if open_fence is None:
            opening = _FENCE_OPEN_RE.match(line)
            if opening:
                open_fence = opening.group(1)
            elif line == "" and (not out or out[-1] == ""):
                continue
        elif line == open_fence:
            open_fence = None
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return out


def finish_document(lines: list[str]) -> str:
    """Join report lines (fenced content verbatim, other blank-line runs collapsed) and redact the text."""
    return redact("\n".join(_collapse_blank_runs_outside_fences(lines)) + "\n")
