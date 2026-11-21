"""C front end of the lexical rules: line splicing, comment/literal blanking, digraphs and ``#if 0`` regions."""

from __future__ import annotations

import bisect
import re
from functools import cached_property

__all__ = ["BIDI_RE", "CSource", "strip_c"]

_WS = r"[^\S\n]*"  # horizontal whitespace (incl. \f, \v)
BIDI_RE = re.compile("[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c]")

# =========================================================================== lexing
_SPLICE_RE = re.compile(r"\\[ \t\f\v]*\r?\n")
_INTERESTING_RE = re.compile(r"[/\"']")
_DIGRAPHS = {"%:%:": "##  ", "<%": "{ ", "%>": "} ", "<:": "[ ", ":>": "] ", "%:": "# "}
_DIGRAPH_RE = re.compile(r"%:%:|<%|%>|<:|:>|%:")
_COND_RE = re.compile(rf"^{_WS}#{_WS}(if|ifdef|ifndef|elif|elifdef|elifndef|else|endif)\b(.*)$")
_CONST01_RE = re.compile(r"^[\s(]*([01])[uUlL]*[\s)]*$")


def _blank(s: str) -> str:
    """Replace everything but newlines by spaces (keeps offsets and lines)."""
    return re.sub(r"[^\n]", " ", s)


def _splice(raw: str) -> tuple[str, list[int], list[int]]:
    """Translation phase 2.  Returns the spliced text and a mapping from
    spliced offsets to original line numbers (``offs``/``lines`` checkpoints)."""
    pieces: list[str] = []
    offs, lines = [0], [1]
    line, out_len, pos = 1, 0, 0

    def note(chunk: str, base: int, line: int) -> int:
        k = chunk.find("\n")
        while k != -1:
            line += 1
            offs.append(base + k + 1)
            lines.append(line)
            k = chunk.find("\n", k + 1)
        return line

    for m in _SPLICE_RE.finditer(raw):
        chunk = raw[pos : m.start()]
        line = note(chunk, out_len, line)
        pieces.append(chunk)
        out_len += len(chunk)
        line += 1
        offs.append(out_len)
        lines.append(line)
        pos = m.end()
    chunk = raw[pos:]
    note(chunk, out_len, line)
    pieces.append(chunk)
    return "".join(pieces), offs, lines


def _lex(s: str) -> tuple[str, str]:
    """Return ``(code, nocomment)``: comments blanked in both; string and
    character literal contents additionally blanked in ``code`` (quotes kept).
    A literal that is not closed on its line ends at the newline, as in the
    compiler, so a stray quote can never hide the following lines."""
    code: list[str] = []
    noc: list[str] = []
    n = len(s)
    i = plain = 0
    while True:
        m = _INTERESTING_RE.search(s, i)
        if m is None:
            break
        i = m.start()
        c = s[i]
        if c == "/":
            nxt = s[i + 1 : i + 2]
            if nxt not in ("*", "/"):
                i += 1
                continue
            if nxt == "*":
                j = s.find("*/", i + 2)
                end = n if j < 0 else j + 2
            else:
                j = s.find("\n", i + 2)
                end = n if j < 0 else j
            code.append(s[plain:i])
            noc.append(s[plain:i])
            b = _blank(s[i:end])
            code.append(b)
            noc.append(b)
            i = plain = end
            continue
        j = i + 1
        closed = False
        while j < n:
            ch = s[j]
            if ch == "\\":
                j += 2
                continue
            if ch == c:
                j += 1
                closed = True
                break
            if ch == "\n":
                break
            j += 1
        j = min(j, n)
        lit = s[i:j]
        code.append(s[plain:i])
        noc.append(s[plain:i])
        inner = lit[1:-1] if closed else lit[1:]
        code.append(c + " " * len(inner) + (c if closed else ""))
        noc.append(lit)
        i = plain = j
    code.append(s[plain:])
    noc.append(s[plain:])
    return "".join(code), "".join(noc)


def _const01(expr: str) -> int | None:
    m = _CONST01_RE.match(expr)
    return int(m.group(1)) if m else None


def _blank_dead_branches(code: str, noc: str) -> tuple[str, str]:
    """Blank ``#if 0`` regions (and the ``#else`` of ``#if 1`` / after an
    ``#elif 1``): the compiler never sees that code, so it must neither count
    as present (an assert moved into ``#if 0`` is a removed assert) nor
    trigger content rules.  Other conditionals are treated as live."""
    code_lines = code.split("\n")
    noc_lines = noc.split("\n")
    stack: list[list[bool]] = []  # [skipping, a branch is certainly taken]
    skipping = 0  # number of frames on the stack that are skipping
    for idx, line in enumerate(code_lines):
        m = _COND_RE.match(line)
        if m:
            d, rest = m.group(1), m.group(2)
            if d in ("if", "ifdef", "ifndef"):
                dead = skipping > 0
                v = _const01(rest) if d == "if" else None
                stack.append([v == 0, v == 1])
                skipping += v == 0
            elif stack:
                top = stack[-1]
                dead = skipping - top[0] > 0  # an *enclosing* group is skipped
                skipping -= top[0]
                if d == "endif":
                    stack.pop()
                elif top[1]:
                    top[0] = True
                elif d == "else":
                    top[0], top[1] = False, True
                else:
                    v = _const01(rest) if d == "elif" else None
                    top[0], top[1] = v == 0, v == 1
                if d != "endif":
                    skipping += top[0]
            else:
                dead = False
        else:
            dead = skipping > 0
        if dead:
            code_lines[idx] = _blank(line)
            noc_lines[idx] = _blank(noc_lines[idx])
    return "\n".join(code_lines), "\n".join(noc_lines)


def _pair_map(s: str) -> dict[int, int]:
    """Offset of every matched ``(``/``[``/``{`` -> offset of its partner,
    computed in one pass (each bracket kind nests independently).  Lets all
    later matching be O(1), so hostile input such as 50 000 unclosed
    ``__attribute__((`` cannot make the checker quadratic."""
    out: dict[int, int] = {}
    stacks: dict[str, list[int]] = {"(": [], "[": [], "{": []}
    partner = {")": "(", "]": "[", "}": "{"}
    for m in re.finditer(r"[()\[\]{}]", s):
        ch = m.group()
        if ch in stacks:
            stacks[ch].append(m.start())
        else:
            st = stacks[partner[ch]]
            if st:
                out[st.pop()] = m.start()
    return out


class CSource:
    """Lexically cleaned views of one C file.

    * ``text``: the file after line splicing;
    * ``code``: comments and literal contents blanked, digraphs canonical,
      dead ``#if 0`` regions blanked - what the content rules match against;
    * ``nocomment``: like ``code`` but with literals kept (header names,
      ``_Pragma`` strings, attribute arguments).

    All three have the same length and line structure, so an offset found
    in one view is valid in the others; :meth:`line_of` maps it back to the
    line number in the original (unspliced) file.
    """

    def __init__(self, raw: str):
        self.raw = raw
        self.text, self._offs, self._lines = _splice(raw)
        code, noc = _lex(self.text)
        code = _DIGRAPH_RE.sub(lambda m: _DIGRAPHS[m.group()], code)
        self.code, self.nocomment = _blank_dead_branches(code, noc)

        self._line_text: dict[int, str] = {}
        self._shown: dict[int, str] = {}

    def line_of(self, offset: int) -> int:
        """Original (unspliced) line number of an offset in the views."""
        return self._lines[bisect.bisect_right(self._offs, offset) - 1]

    def raw_line_of(self, offset: int) -> int:
        """Line number of an offset in :attr:`raw`."""
        return bisect.bisect_right(self._raw_newlines, offset - 1) + 1

    @cached_property
    def _code_newlines(self) -> list[int]:
        return [m.start() for m in re.finditer("\n", self.code)]

    @cached_property
    def _raw_newlines(self) -> list[int]:
        return [m.start() for m in re.finditer("\n", self.raw)]

    def line_text(self, offset: int) -> str:
        """Whitespace-normalised code line containing ``offset`` (cached per
        line, so many findings on one huge line stay linear)."""
        nl = self._code_newlines
        k = bisect.bisect_left(nl, offset)
        if k not in self._line_text:
            start = nl[k - 1] + 1 if k > 0 else 0
            end = nl[k] if k < len(nl) else len(self.code)
            self._line_text[k] = " ".join(self.code[start:end].split())[:500]
        return self._line_text[k]

    @cached_property
    def _raw_lines(self) -> list[str]:
        return self.raw.split("\n")

    def shown_line(self, line: int) -> str:
        """Original source line ``line`` for messages (whitespace collapsed,
        control/bidi characters escaped, truncated)."""
        if line not in self._shown:
            lines = self._raw_lines
            text = " ".join((lines[line - 1] if 1 <= line <= len(lines) else "")[:1000].split())
            text = "".join(c if c.isprintable() and not BIDI_RE.match(c) else f"\\u{ord(c):04x}" for c in text)
            self._shown[line] = text if len(text) <= 120 else text[:117] + "..."
        return self._shown[line]

    @cached_property
    def pairs(self) -> dict[int, int]:
        """Bracket partners in :attr:`code`."""
        return _pair_map(self.code)

    @cached_property
    def pairs_nodirectives(self) -> dict[int, int]:
        """Bracket partners in :attr:`code_nodirectives`."""
        return _pair_map(self.code_nodirectives)

    @cached_property
    def code_nodirectives(self) -> str:
        """``code`` with preprocessor directive lines blanked (brace matching
        must not be confused by ``#define BEGIN {``)."""
        return "\n".join(_blank(ln) if ln.lstrip().startswith("#") else ln for ln in self.code.split("\n"))


def strip_c(text: str) -> str:
    """Comment- and literal-free view of C source (same line structure);
    see :class:`CSource`."""
    return CSource(text).code
