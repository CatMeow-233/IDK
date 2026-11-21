"""C++ lexical forms (digit separators, raw string literals) rewritten so the C front end reads C++ files."""

from __future__ import annotations

import re

from leanc.lang.c_lexer import CSource

__all__ = ["CppSource", "cpp_normalize"]

# One pattern for the tokens that matter to the normaliser, tried in order.
_NORM_RE = re.compile(
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


def cpp_normalize(text: str) -> str:
    """Rewrite C++-only lexical forms so the C lexer of
    :class:`~leanc.lang.c_lexer.CSource` reads the file correctly, keeping
    every offset and line break:

    * digit separators (``1'000'000``) become ``0`` digits (a ``'`` would
      otherwise open a character literal and blank the rest of the line);
    * raw string literals (``R"x(...)x"``, possibly multi-line) become ``""``
      followed by blanks (the C lexer would end them at the first newline and
      treat the next line as a string, which could hide code).
    """
    out: list[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _NORM_RE.search(text, pos)
        if m is None:
            break
        kind = m.lastgroup
        if kind == "raw":
            start = m.start()
            dm = _RAW_DELIM_RE.match(text, m.end())
            if dm is None:  # not a valid raw string: leave it to the C lexer
                out.append(text[pos : m.end()])
                pos = m.end()
                continue
            delim = dm.group()[:-1]
            close = text.find(")" + delim + '"', dm.end())
            end = n if close < 0 else close + len(delim) + 2
            out.append(text[pos:start])
            lit = text[start:end]
            out.append('""' + re.sub(r"[^\n]", " ", lit[2:]) if len(lit) >= 2 else " " * len(lit))
            pos = end
            continue
        out.append(text[pos : m.start()])
        tok = m.group()
        if kind == "num" and "'" in tok:
            tok = tok.replace("'", "0")
        out.append(tok)
        pos = m.end()
    out.append(text[pos:])
    res = "".join(out)
    assert len(res) == len(text)
    return res


class CppSource(CSource):
    """:class:`CSource` views of a C++ file (after :func:`cpp_normalize`);
    ``raw`` stays the original text, so reported lines show the real code."""

    def __init__(self, raw: str):
        super().__init__(cpp_normalize(raw))
        self.raw = raw
