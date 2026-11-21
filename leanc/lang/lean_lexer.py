"""Lean 4 lexer: comments and string / char literal contents blanked, hiding constructs recorded."""

from __future__ import annotations

import bisect
import re

__all__ = ["LeanSource"]

_CHAR_LIT_RE = re.compile(r"'(?:\\(?:u\{[0-9a-fA-F]+\}|x[0-9a-fA-F]{2}|.)|[^\\'\n])'")
_RAW_STR_RE = re.compile(r'r(#*)"')


class LeanSource:
    """A Lean file with comments and literal contents blanked (same length,
    newlines preserved, so offsets map to the original lines)."""

    def __init__(self, raw: str):
        self.raw = raw
        self.obfuscation: list[tuple[int, str]] = []  # (offset, what)
        self.code = self._lex(raw)
        self._nl = [i for i, ch in enumerate(raw) if ch == "\n"]

    def line_of(self, offset: int) -> int:
        return bisect.bisect_right(self._nl, offset - 1) + 1

    def shown(self, offset: int) -> str:
        start = self.raw.rfind("\n", 0, offset) + 1
        end = self.raw.find("\n", offset)
        line = self.raw[start : end if end >= 0 else len(self.raw)].strip()
        return line if len(line) <= 120 else line[:117] + "..."

    @staticmethod
    def _blank(s: str) -> str:
        return "".join("\n" if c == "\n" else " " for c in s)

    def _lex(self, s: str) -> str:
        out: list[str] = []
        i, n = 0, len(s)
        while i < n:
            c = s[i]
            if c == "-" and s.startswith("--", i):
                j = s.find("\n", i)
                j = n if j < 0 else j
                out.append(self._blank(s[i:j]))
                i = j
                continue
            if c == "/" and s.startswith("/-", i):
                depth, j = 1, i + 2
                while j < n and depth:
                    if s.startswith("/-", j):
                        depth, j = depth + 1, j + 2
                    elif s.startswith("-/", j):
                        depth, j = depth - 1, j + 2
                    else:
                        j += 1
                out.append(self._blank(s[i:j]))
                i = j
                continue
            if c == "«":
                self.obfuscation.append((i, "a guillemet-quoted identifier («...»)"))
                j = s.find("»", i)
                j = n if j < 0 else j + 1
                out.append(s[i:j])
                i = j
                continue
            prev = s[i - 1] if i else ""
            if c == "r" and not (prev.isalnum() or prev in "_'!?."):
                m = _RAW_STR_RE.match(s, i)
                if m:
                    close = '"' + m.group(1)
                    end = s.find(close, m.end())
                    body_end = n if end < 0 else end
                    out.append(s[i : m.end()] + self._blank(s[m.end() : body_end]) + s[body_end : body_end + len(close)])
                    i = min(n, body_end + len(close))
                    continue
            if c == '"':
                if prev == "!":
                    self.obfuscation.append((i, 'an interpolated string (s!"..{..}.." / m!"..." / f!"...")'))
                j = i + 1
                while j < n and s[j] != '"':
                    j += 2 if s[j] == "\\" else 1
                j = min(j, n)
                out.append('"' + self._blank(s[i + 1 : j]) + ('"' if j < n else ""))
                i = j + 1
                continue
            if c == "'" and not (prev.isalnum() or prev in "_'!?"):
                m = _CHAR_LIT_RE.match(s, i)
                if m:
                    out.append("'" + self._blank(m.group(0)[1:-1]) + "'")
                    i = m.end()
                    continue
            out.append(c)
            i += 1
        return "".join(out)
