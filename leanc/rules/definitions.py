"""File-scope function definitions of C code, shared by the integrity, ACSL and repair-policy rules."""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

from leanc.lang.c_lexer import CSource

__all__ = [
    "NOT_A_FUNCTION",
    "FunctionDefinition",
    "defined_functions",
    "find_function_definitions",
    "is_trivial_body",
]


def _match_close(s: str, open_at: int, open_ch: str = "(", close_ch: str = ")") -> int | None:
    depth = 0
    for k in range(open_at, len(s)):
        ch = s[k]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return k
    return None


@dataclass(frozen=True)
class FunctionDefinition:
    file: str
    line: int
    body: str  # code view of the body between the braces
    trivial: bool


# =========================================================================== target function
_TYPE_WORDS = (
    r"const|volatile|restrict|signed|unsigned|short|long|int|char|_Bool|bool|float|double|void|"
    r"size_t|ssize_t|ptrdiff_t|intptr_t|uintptr_t|[A-Za-z_]\w*_t|struct\s+\w+|enum\s+\w+|union\s+\w+"
)
_CAST_RE = re.compile(rf"^\(\s*(?:(?:{_TYPE_WORDS})\s*)+\**\s*\)")
_CONST_RE = re.compile(
    r"(?:0[xX][0-9a-fA-F]+|0[bB][01]+|\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?)[uUlLfF]*"
    r"|'[^'\n]*'|(?:L|u8|u|U)?\"[^\"\n]*\"|NULL|nullptr|true|false|[A-Z_][A-Z0-9_]+"
)
_MAX_TRIVIAL_BODY = 2000
# a no-op statement `(void)param;` / `(void)(param);` (silences -Wunused-parameter)
_VOID_CAST_STMT_RE = re.compile(r"\(\s*void\s*\)\s*(?:[A-Za-z_]\w*|\(\s*[A-Za-z_]\w*\s*\))\s*;")
_DEF_ATTR_RE = re.compile(r"__attribute__\s*\(|__declspec\s*\(")
_NORETURN_RE = re.compile(
    r"(?:abort|exit|_Exit|quick_exit|__builtin_trap)\s*\([^;]*\)\s*;(?:\s*return\b[^;]*;)?"
)


def _is_constant_expr(expr: str) -> bool:
    e = expr.strip()
    for _ in range(32):
        e = e.strip()
        if e.startswith("(") and _match_close(e, 0) == len(e) - 1:
            e = e[1:-1]
            continue
        m = _CAST_RE.match(e)
        if m and m.end() < len(e):
            e = e[m.end() :]
            continue
        if e[:1] in ("-", "+", "~", "!"):
            e = e[1:]
            continue
        break
    return bool(_CONST_RE.fullmatch(e.strip()))


def is_trivial_body(body: str) -> bool:
    """Heuristic: does a function body (code view, without the outer braces)
    do nothing but return a literal / macro constant (or nothing at all, or
    only call abort/exit)?  ``(void)param;`` casts and extra braces or
    semicolons are ignored, however many there are."""
    s = " ".join(_VOID_CAST_STMT_RE.sub(" ", body).split())
    if len(s) > _MAX_TRIVIAL_BODY:
        return False  # a stub is short; this also bounds the work below
    while True:
        s2 = s.lstrip("; ").strip()
        if s2.startswith("{") and _match_close(s2, 0, "{", "}") == len(s2) - 1:
            s2 = s2[1:-1].strip()
        if s2 == s:
            break
        s = s2
    s = re.sub(r";[\s;]*$", ";", s)
    if s == "" or re.fullmatch(r"return\s*;", s):
        return True
    if _NORETURN_RE.fullmatch(s):
        return True
    m = re.fullmatch(r"return\b(.*);", s, re.DOTALL)
    return bool(m and ";" not in m.group(1) and _is_constant_expr(m.group(1)))


def find_function_definitions(src: CSource, name: str, file: str = "") -> list[FunctionDefinition]:
    """File-scope definitions (with a body) of function ``name``.

    Declarations, calls, macro-generated definitions and code in ``#if 0``
    are not definitions; the conservative consequence is that hiding the
    target behind a macro reads as "definition removed"."""
    code = src.code_nodirectives
    pairs = src.pairs_nodirectives
    marks: list[int] = []
    depth_after: list[int] = []
    d = 0
    for bm in re.finditer(r"[{}]", code):
        d += 1 if bm.group() == "{" else -1
        marks.append(bm.start())
        depth_after.append(d)

    def depth_at(off: int) -> int:
        j = bisect.bisect_left(marks, off) - 1
        return depth_after[j] if j >= 0 else 0

    out: list[FunctionDefinition] = []
    for m in re.finditer(rf"(?<![\w.])(?<!->)\b{re.escape(name)}\s*\(", code):
        if depth_at(m.start()) > 0:
            continue
        close = pairs.get(m.end() - 1)
        if close is None:
            continue
        j = close + 1
        for _ in range(64):  # skip attributes between ')' and '{'
            while j < len(code) and code[j].isspace():
                j += 1
            am = _DEF_ATTR_RE.match(code, j)
            if am and (c2 := pairs.get(am.end() - 1)) is not None:
                j = c2 + 1
                continue
            if code.startswith("[[", j) and (c2 := pairs.get(j)) is not None:
                j = c2 + 1
                continue
            break
        if j < len(code) and code[j] == "{":
            end = pairs.get(j)
            body = code[j + 1 : end if end is not None else len(code)]
            out.append(FunctionDefinition(file, src.line_of(m.start()), body, is_trivial_body(body)))
    return out


_DEF_CANDIDATE_RE = re.compile(r"(?<![\w.])(?<!->)\b([A-Za-z_]\w*)\s*\(")
NOT_A_FUNCTION = frozenset(
    {"__attribute__", "__attribute", "__declspec", "sizeof", "_Alignas", "alignas", "_Alignof", "alignof",
     "__typeof__", "__typeof", "typeof", "_Static_assert", "static_assert", "__extension__", "_Generic",
     "__asm__", "__asm", "asm", "if", "while", "for", "switch", "return"}
)  # fmt: skip


def defined_functions(src: CSource) -> list[tuple[str, int]]:
    """Names (and offsets) of all file-scope function definitions."""
    code = src.code_nodirectives
    pairs = src.pairs_nodirectives
    marks: list[int] = []
    depth_after: list[int] = []
    d = 0
    for bm in re.finditer(r"[{}]", code):
        d += 1 if bm.group() == "{" else -1
        marks.append(bm.start())
        depth_after.append(d)
    out: list[tuple[str, int]] = []
    for m in _DEF_CANDIDATE_RE.finditer(code):
        name = m.group(1)
        if name in NOT_A_FUNCTION:
            continue
        j = bisect.bisect_left(marks, m.start()) - 1
        if j >= 0 and depth_after[j] > 0:
            continue
        close = pairs.get(m.end() - 1)
        if close is None:
            continue
        k = close + 1
        for _ in range(64):
            while k < len(code) and code[k].isspace():
                k += 1
            am = re.match(r"(?:__attribute__|__attribute|__declspec)\s*\(", code[k : k + 40])
            if am and (c2 := pairs.get(k + am.end() - 1)) is not None:
                k = c2 + 1
                continue
            if code.startswith("[[", k) and (c2 := pairs.get(k)) is not None:
                k = c2 + 1
                continue
            break
        if k < len(code) and code[k] == "{":
            out.append((name, m.start()))
    return out
