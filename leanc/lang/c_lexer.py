from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from functools import cached_property
from typing import NamedTuple

__all__ = ["BIDI_RE", "CSource", "strip_c"]

_HORIZONTAL_WHITESPACE = r"[^\S\n]*"
BIDI_RE = re.compile("[‪-‮⁦-⁩‎‏؜]")

_LINE_SPLICE_RE = re.compile(r"\\[ \t\f\v]*\r?\n")
_COMMENT_OR_LITERAL_START_RE = re.compile(r"[/\"']")
_DIGRAPHS = {"%:%:": "##  ", "<%": "{ ", "%>": "} ", "<:": "[ ", ":>": "] ", "%:": "# "}
_DIGRAPH_RE = re.compile(r"%:%:|<%|%>|<:|:>|%:")
_CONDITIONAL_DIRECTIVE_RE = re.compile(
    rf"^{_HORIZONTAL_WHITESPACE}#{_HORIZONTAL_WHITESPACE}"
    r"(if|ifdef|ifndef|elif|elifdef|elifndef|else|endif)\b(.*)$"
)
_CONSTANT_ZERO_OR_ONE_RE = re.compile(r"^[\s(]*([01])[uUlL]*[\s)]*$")
_OPENING_DIRECTIVES = ("if", "ifdef", "ifndef")
_BRACKET_RE = re.compile(r"[()\[\]{}]")
_OPENING_BRACKET_OF = {")": "(", "]": "[", "}": "{"}

MAX_LINE_TEXT_CHARS = 500
MAX_RAW_LINE_CHARS = 1000
MAX_SHOWN_LINE_CHARS = 120
ELLIPSIS = "..."


class _Spliced(NamedTuple):
    text: str
    checkpoint_offsets: list[int]
    checkpoint_lines: list[int]


class _Views(NamedTuple):
    code: str
    nocomment: str


@dataclass
class _ConditionalFrame:
    skipping: bool
    branch_certainly_taken: bool


def _blank_keeping_newlines(s: str) -> str:
    return re.sub(r"[^\n]", " ", s)


def _splice_lines(raw: str) -> _Spliced:
    pieces: list[str] = []
    offsets, lines = [0], [1]
    line, out_len, pos = 1, 0, 0

    def record_newlines(chunk: str, base: int, line: int) -> int:
        k = chunk.find("\n")
        while k != -1:
            line += 1
            offsets.append(base + k + 1)
            lines.append(line)
            k = chunk.find("\n", k + 1)
        return line

    for splice in _LINE_SPLICE_RE.finditer(raw):
        chunk = raw[pos : splice.start()]
        line = record_newlines(chunk, out_len, line)
        pieces.append(chunk)
        out_len += len(chunk)
        line += 1
        offsets.append(out_len)
        lines.append(line)
        pos = splice.end()
    chunk = raw[pos:]
    record_newlines(chunk, out_len, line)
    pieces.append(chunk)
    return _Spliced("".join(pieces), offsets, lines)


def _comment_end(s: str, start: int) -> int | None:
    marker = s[start + 1 : start + 2]
    if marker == "*":
        close = s.find("*/", start + 2)
        return len(s) if close < 0 else close + 2
    if marker == "/":
        newline = s.find("\n", start + 2)
        return len(s) if newline < 0 else newline
    return None


def _literal_end_on_its_line(s: str, start: int) -> tuple[int, bool]:
    quote = s[start]
    j = start + 1
    while j < len(s):
        ch = s[j]
        if ch == "\\":
            j += 2
            continue
        if ch == quote:
            return j + 1, True
        if ch == "\n":
            break
        j += 1
    return min(j, len(s)), False


def _blank_comments_and_literal_contents(s: str) -> _Views:
    code: list[str] = []
    nocomment: list[str] = []
    pos = plain = 0
    while (match := _COMMENT_OR_LITERAL_START_RE.search(s, pos)) is not None:
        start = match.start()
        before = s[plain:start]
        if s[start] == "/":
            end = _comment_end(s, start)
            if end is None:
                pos = start + 1
                continue
            blanked = _blank_keeping_newlines(s[start:end])
            code += [before, blanked]
            nocomment += [before, blanked]
        else:
            end, closed = _literal_end_on_its_line(s, start)
            quote, literal = s[start], s[start:end]
            inner = literal[1:-1] if closed else literal[1:]
            code += [before, quote + " " * len(inner) + (quote if closed else "")]
            nocomment += [before, literal]
        pos = plain = end
    code.append(s[plain:])
    nocomment.append(s[plain:])
    return _Views("".join(code), "".join(nocomment))


def _constant_zero_or_one(expr: str) -> int | None:
    match = _CONSTANT_ZERO_OR_ONE_RE.match(expr)
    return int(match.group(1)) if match else None


class _ConditionalStack:
    def __init__(self) -> None:
        self._frames: list[_ConditionalFrame] = []
        self._skipping_frames = 0

    def line_is_dead(self, line: str) -> bool:
        directive = _CONDITIONAL_DIRECTIVE_RE.match(line)
        if directive is None:
            return self._skipping_frames > 0
        name, condition = directive.group(1), directive.group(2)
        if name in _OPENING_DIRECTIVES:
            return self._open_group(name, condition)
        if self._frames:
            return self._switch_or_close_group(name, condition)
        return False

    def _open_group(self, name: str, condition: str) -> bool:
        inside_skipped_group = self._skipping_frames > 0
        value = _constant_zero_or_one(condition) if name == "if" else None
        frame = _ConditionalFrame(skipping=value == 0, branch_certainly_taken=value == 1)
        self._frames.append(frame)
        self._skipping_frames += frame.skipping
        return inside_skipped_group

    def _switch_or_close_group(self, name: str, condition: str) -> bool:
        frame = self._frames[-1]
        enclosing_group_skipped = self._skipping_frames - frame.skipping > 0
        self._skipping_frames -= frame.skipping
        if name == "endif":
            self._frames.pop()
            return enclosing_group_skipped
        if frame.branch_certainly_taken:
            frame.skipping = True
        elif name == "else":
            frame.skipping, frame.branch_certainly_taken = False, True
        else:
            value = _constant_zero_or_one(condition) if name == "elif" else None
            frame.skipping, frame.branch_certainly_taken = value == 0, value == 1
        self._skipping_frames += frame.skipping
        return enclosing_group_skipped


def _blank_never_compiled_branches(code: str, nocomment: str) -> _Views:
    code_lines = code.split("\n")
    nocomment_lines = nocomment.split("\n")
    conditionals = _ConditionalStack()
    for index, line in enumerate(code_lines):
        if conditionals.line_is_dead(line):
            code_lines[index] = _blank_keeping_newlines(line)
            nocomment_lines[index] = _blank_keeping_newlines(nocomment_lines[index])
    return _Views("\n".join(code_lines), "\n".join(nocomment_lines))


def _bracket_partners_in_one_pass(s: str) -> dict[int, int]:
    out: dict[int, int] = {}
    open_offsets: dict[str, list[int]] = {"(": [], "[": [], "{": []}
    for match in _BRACKET_RE.finditer(s):
        bracket = match.group()
        if bracket in open_offsets:
            open_offsets[bracket].append(match.start())
        else:
            pending = open_offsets[_OPENING_BRACKET_OF[bracket]]
            if pending:
                out[pending.pop()] = match.start()
    return out


def _shortened_for_display(text: str) -> str:
    if len(text) <= MAX_SHOWN_LINE_CHARS:
        return text
    return text[: MAX_SHOWN_LINE_CHARS - len(ELLIPSIS)] + ELLIPSIS


def _escape_invisible_characters(text: str) -> str:
    return "".join(c if c.isprintable() and not BIDI_RE.match(c) else f"\\u{ord(c):04x}" for c in text)


class CSource:
    def __init__(self, raw: str):
        self.raw = raw
        self.text, self._offs, self._lines = _splice_lines(raw)
        code, nocomment = _blank_comments_and_literal_contents(self.text)
        code = _DIGRAPH_RE.sub(lambda m: _DIGRAPHS[m.group()], code)
        self.code, self.nocomment = _blank_never_compiled_branches(code, nocomment)

        self._line_text: dict[int, str] = {}
        self._shown: dict[int, str] = {}

    def line_of(self, offset: int) -> int:
        return self._lines[bisect.bisect_right(self._offs, offset) - 1]

    def raw_line_of(self, offset: int) -> int:
        return bisect.bisect_right(self._raw_newlines, offset - 1) + 1

    @cached_property
    def _code_newlines(self) -> list[int]:
        return [m.start() for m in re.finditer("\n", self.code)]

    @cached_property
    def _raw_newlines(self) -> list[int]:
        return [m.start() for m in re.finditer("\n", self.raw)]

    def line_text(self, offset: int) -> str:
        newlines = self._code_newlines
        k = bisect.bisect_left(newlines, offset)
        if k not in self._line_text:
            start = newlines[k - 1] + 1 if k > 0 else 0
            end = newlines[k] if k < len(newlines) else len(self.code)
            self._line_text[k] = " ".join(self.code[start:end].split())[:MAX_LINE_TEXT_CHARS]
        return self._line_text[k]

    @cached_property
    def _raw_lines(self) -> list[str]:
        return self.raw.split("\n")

    def shown_line(self, line: int) -> str:
        if line not in self._shown:
            lines = self._raw_lines
            original = lines[line - 1] if 1 <= line <= len(lines) else ""
            text = _escape_invisible_characters(" ".join(original[:MAX_RAW_LINE_CHARS].split()))
            self._shown[line] = _shortened_for_display(text)
        return self._shown[line]

    @cached_property
    def pairs(self) -> dict[int, int]:
        return _bracket_partners_in_one_pass(self.code)

    @cached_property
    def pairs_nodirectives(self) -> dict[int, int]:
        return _bracket_partners_in_one_pass(self.code_nodirectives)

    @cached_property
    def code_nodirectives(self) -> str:
        return "\n".join(
            _blank_keeping_newlines(ln) if ln.lstrip().startswith("#") else ln for ln in self.code.split("\n")
        )


def strip_c(text: str) -> str:
    return CSource(text).code
