from __future__ import annotations

import bisect
import re
from typing import NamedTuple

__all__ = ["LeanSource", "ObfuscationSite"]

_CHAR_LIT_RE = re.compile(r"'(?:\\(?:u\{[0-9a-fA-F]+\}|x[0-9a-fA-F]{2}|.)|[^\\'\n])'")
_RAW_STR_RE = re.compile(r'r(#*)"')
_IDENTIFIER_TAIL_BEFORE_RAW_STRING = "_'!?."
_IDENTIFIER_TAIL_BEFORE_CHAR_LITERAL = "_'!?"
MAX_SHOWN_LINE_CHARS = 120
ELLIPSIS = "..."

GUILLEMET_IDENTIFIER = "a guillemet-quoted identifier («...»)"
INTERPOLATED_STRING = 'an interpolated string (s!"..{..}.." / m!"..." / f!"...")'


class ObfuscationSite(NamedTuple):
    offset: int
    what: str


def _blank_keeping_newlines(s: str) -> str:
    return "".join("\n" if c == "\n" else " " for c in s)


def _continues_identifier(prev: str, tail_chars: str) -> bool:
    return prev.isalnum() or prev in tail_chars


def _line_comment_end(s: str, start: int) -> int:
    newline = s.find("\n", start)
    return len(s) if newline < 0 else newline


def _nested_block_comment_end(s: str, start: int) -> int:
    depth, j = 1, start + 2
    while j < len(s) and depth:
        if s.startswith("/-", j):
            depth, j = depth + 1, j + 2
        elif s.startswith("-/", j):
            depth, j = depth - 1, j + 2
        else:
            j += 1
    return j


class LeanSource:
    def __init__(self, raw: str):
        self.raw = raw
        self.obfuscation: list[ObfuscationSite] = []
        self.code = self._lex(raw)
        self._newline_offsets = [i for i, ch in enumerate(raw) if ch == "\n"]

    def line_of(self, offset: int) -> int:
        return bisect.bisect_right(self._newline_offsets, offset - 1) + 1

    def shown(self, offset: int) -> str:
        start = self.raw.rfind("\n", 0, offset) + 1
        end = self.raw.find("\n", offset)
        line = self.raw[start : end if end >= 0 else len(self.raw)].strip()
        if len(line) <= MAX_SHOWN_LINE_CHARS:
            return line
        return line[: MAX_SHOWN_LINE_CHARS - len(ELLIPSIS)] + ELLIPSIS

    def _lex(self, s: str) -> str:
        out: list[str] = []
        i, n = 0, len(s)
        while i < n:
            token = self._blanked_token_at(s, i)
            if token is None:
                out.append(s[i])
                i += 1
                continue
            text, i = token
            out.append(text)
        return "".join(out)

    def _blanked_token_at(self, s: str, i: int) -> tuple[str, int] | None:
        c = s[i]
        prev = s[i - 1] if i else ""
        if s.startswith("--", i):
            end = _line_comment_end(s, i)
            return _blank_keeping_newlines(s[i:end]), end
        if s.startswith("/-", i):
            end = _nested_block_comment_end(s, i)
            return _blank_keeping_newlines(s[i:end]), end
        if c == "«":
            return self._guillemet_identifier(s, i)
        if c == "r" and not _continues_identifier(prev, _IDENTIFIER_TAIL_BEFORE_RAW_STRING):
            return self._raw_string(s, i)
        if c == '"':
            return self._string(s, i, prev)
        if c == "'" and not _continues_identifier(prev, _IDENTIFIER_TAIL_BEFORE_CHAR_LITERAL):
            return self._char_literal(s, i)
        return None

    def _guillemet_identifier(self, s: str, i: int) -> tuple[str, int]:
        self.obfuscation.append(ObfuscationSite(i, GUILLEMET_IDENTIFIER))
        close = s.find("»", i)
        end = len(s) if close < 0 else close + 1
        return s[i:end], end

    @staticmethod
    def _raw_string(s: str, i: int) -> tuple[str, int] | None:
        opening = _RAW_STR_RE.match(s, i)
        if opening is None:
            return None
        close = '"' + opening.group(1)
        found = s.find(close, opening.end())
        body_end = len(s) if found < 0 else found
        text = s[i : opening.end()] + _blank_keeping_newlines(s[opening.end() : body_end]) + s[body_end : body_end + len(close)]
        return text, min(len(s), body_end + len(close))

    def _string(self, s: str, i: int, prev: str) -> tuple[str, int]:
        if prev == "!":
            self.obfuscation.append(ObfuscationSite(i, INTERPOLATED_STRING))
        n = len(s)
        j = i + 1
        while j < n and s[j] != '"':
            j += 2 if s[j] == "\\" else 1
        j = min(j, n)
        return '"' + _blank_keeping_newlines(s[i + 1 : j]) + ('"' if j < n else ""), j + 1

    @staticmethod
    def _char_literal(s: str, i: int) -> tuple[str, int] | None:
        literal = _CHAR_LIT_RE.match(s, i)
        if literal is None:
            return None
        return "'" + _blank_keeping_newlines(literal.group(0)[1:-1]) + "'", literal.end()
