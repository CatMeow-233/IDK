"""C++ lexical forms (digit separators, raw string literals) rewritten so the C front end reads C++ files."""

from __future__ import annotations

import re

from leanc.lang.c_lexer import CSource

__all__ = ["CppSource", "cpp_normalize"]

_NORMALISER_TOKENS_IN_PRIORITY_ORDER_RE = re.compile(
    r"(?P<lc>//[^\n]*)"
    r"|(?P<bc>/\*.*?(?:\*/|\Z))"
    r'|(?P<raw>(?<![\w])(?:u8|u|U|L)?R")'
    r'|(?P<str>"(?:[^"\\\n]|\\.)*"?)'
    r"|(?P<chr>'(?:[^'\\\n]|\\.)*'?)"
    r"|(?P<num>(?<![\w.])\.?\d(?:[\w.]|'(?=\w)|[eEpP][+-])*)"
    r"|(?P<id>[A-Za-z_]\w*)",
    re.S,
)
_RAW_DELIM_RE = re.compile(r'[^\s()\\"]{0,16}\(')


def _valid_raw_string_end(text: str, delimiter_start: int) -> int | None:
    delimiter_match = _RAW_DELIM_RE.match(text, delimiter_start)
    if delimiter_match is None:
        return None
    closing = ")" + delimiter_match.group()[:-1] + '"'
    close = text.find(closing, delimiter_match.end())
    return len(text) if close < 0 else close + len(closing)


def _raw_string_as_empty_c_string(literal: str) -> str:
    if len(literal) < 2:
        return " " * len(literal)
    return '""' + re.sub(r"[^\n]", " ", literal[2:])


def _digit_separators_as_zeros(number: str) -> str:
    return number.replace("'", "0")


def cpp_normalize(text: str) -> str:
    """Rewrite C++-only digit separators and raw strings into C-lexable text, keeping every offset and line break."""
    out: list[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        match = _NORMALISER_TOKENS_IN_PRIORITY_ORDER_RE.search(text, pos)
        if match is None:
            break
        kind = match.lastgroup
        if kind == "raw":
            end = _valid_raw_string_end(text, match.end())
            if end is None:
                out.append(text[pos : match.end()])
                pos = match.end()
                continue
            out.append(text[pos : match.start()])
            out.append(_raw_string_as_empty_c_string(text[match.start() : end]))
            pos = end
            continue
        out.append(text[pos : match.start()])
        token = match.group()
        if kind == "num" and "'" in token:
            token = _digit_separators_as_zeros(token)
        out.append(token)
        pos = match.end()
    out.append(text[pos:])
    normalised = "".join(out)
    assert len(normalised) == len(text)
    return normalised


class CppSource(CSource):
    """:class:`CSource` views of a C++ file after :func:`cpp_normalize`; ``raw`` stays the original text."""

    def __init__(self, raw: str):
        super().__init__(cpp_normalize(raw))
        self.raw = raw
