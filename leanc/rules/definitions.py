from __future__ import annotations

import bisect
import re
from collections.abc import Callable
from dataclasses import dataclass

from leanc.lang.c_lexer import CSource

__all__ = [
    "NOT_A_FUNCTION",
    "DefinitionSite",
    "FunctionDefinition",
    "defined_functions",
    "find_function_definitions",
    "function_definition_sites",
    "is_trivial_body",
]

_MAX_TRIVIAL_BODY_CHARS = 2000
_MAX_EXPRESSION_UNWRAPS = 32
_MAX_ATTRIBUTE_LISTS = 64
_ATTRIBUTE_LOOKAHEAD_CHARS = 40

_TYPE_WORDS = (
    r"const|volatile|restrict|signed|unsigned|short|long|int|char|_Bool|bool|float|double|void|"
    r"size_t|ssize_t|ptrdiff_t|intptr_t|uintptr_t|[A-Za-z_]\w*_t|struct\s+\w+|enum\s+\w+|union\s+\w+"
)
_CAST_RE = re.compile(rf"^\(\s*(?:(?:{_TYPE_WORDS})\s*)+\**\s*\)")
_CONST_RE = re.compile(
    r"(?:0[xX][0-9a-fA-F]+|0[bB][01]+|\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?)[uUlLfF]*"
    r"|'[^'\n]*'|(?:L|u8|u|U)?\"[^\"\n]*\"|NULL|nullptr|true|false|[A-Z_][A-Z0-9_]+"
)
_UNUSED_PARAMETER_CAST_RE = re.compile(r"\(\s*void\s*\)\s*(?:[A-Za-z_]\w*|\(\s*[A-Za-z_]\w*\s*\))\s*;")
_TARGET_ATTRIBUTE_RE = re.compile(r"__attribute__\s*\(|__declspec\s*\(")
_ANY_ATTRIBUTE_RE = re.compile(r"(?:__attribute__|__attribute|__declspec)\s*\(")
_NORETURN_CALL_RE = re.compile(
    r"(?:abort|exit|_Exit|quick_exit|__builtin_trap)\s*\([^;]*\)\s*;(?:\s*return\b[^;]*;)?"
)
_UNARY_PREFIXES = ("-", "+", "~", "!")
_DEF_CANDIDATE_RE = re.compile(r"(?<![\w.])(?<!->)\b([A-Za-z_]\w*)\s*\(")
NOT_A_FUNCTION = frozenset(
    {"__attribute__", "__attribute", "__declspec", "sizeof", "_Alignas", "alignas", "_Alignof", "alignof",
     "__typeof__", "__typeof", "typeof", "_Static_assert", "static_assert", "__extension__", "_Generic",
     "__asm__", "__asm", "asm", "if", "while", "for", "switch", "return"}
)


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
    body: str
    trivial: bool


@dataclass(frozen=True)
class DefinitionSite:
    name: str
    offset: int
    body_open: int


def _is_constant_expr(expr: str) -> bool:
    e = expr.strip()
    for _ in range(_MAX_EXPRESSION_UNWRAPS):
        e = e.strip()
        if e.startswith("(") and _match_close(e, 0) == len(e) - 1:
            e = e[1:-1]
            continue
        cast = _CAST_RE.match(e)
        if cast and cast.end() < len(e):
            e = e[cast.end() :]
            continue
        if e[:1] in _UNARY_PREFIXES:
            e = e[1:]
            continue
        break
    return bool(_CONST_RE.fullmatch(e.strip()))


def _without_redundant_braces_and_semicolons(statements: str) -> str:
    s = statements
    while True:
        unwrapped = s.lstrip("; ").strip()
        if unwrapped.startswith("{") and _match_close(unwrapped, 0, "{", "}") == len(unwrapped) - 1:
            unwrapped = unwrapped[1:-1].strip()
        if unwrapped == s:
            return s
        s = unwrapped


def is_trivial_body(body: str) -> bool:
    s = " ".join(_UNUSED_PARAMETER_CAST_RE.sub(" ", body).split())
    if len(s) > _MAX_TRIVIAL_BODY_CHARS:
        return False
    s = _without_redundant_braces_and_semicolons(s)
    s = re.sub(r";[\s;]*$", ";", s)
    if s == "" or re.fullmatch(r"return\s*;", s):
        return True
    if _NORETURN_CALL_RE.fullmatch(s):
        return True
    returned = re.fullmatch(r"return\b(.*);", s, re.DOTALL)
    return bool(returned and ";" not in returned.group(1) and _is_constant_expr(returned.group(1)))


def _brace_depth_finder(code: str) -> Callable[[int], int]:
    marks: list[int] = []
    depth_after: list[int] = []
    depth = 0
    for brace in re.finditer(r"[{}]", code):
        depth += 1 if brace.group() == "{" else -1
        marks.append(brace.start())
        depth_after.append(depth)

    def depth_at(offset: int) -> int:
        j = bisect.bisect_left(marks, offset) - 1
        return depth_after[j] if j >= 0 else 0

    return depth_at


def _target_attribute_paren(code: str, j: int) -> int | None:
    attribute = _TARGET_ATTRIBUTE_RE.match(code, j)
    return attribute.end() - 1 if attribute else None


def _any_attribute_paren(code: str, j: int) -> int | None:
    attribute = _ANY_ATTRIBUTE_RE.match(code[j : j + _ATTRIBUTE_LOOKAHEAD_CHARS])
    return j + attribute.end() - 1 if attribute else None


def _skip_attributes(
    code: str, pairs: dict[int, int], j: int, attribute_paren: Callable[[str, int], int | None]
) -> int:
    for _ in range(_MAX_ATTRIBUTE_LISTS):
        while j < len(code) and code[j].isspace():
            j += 1
        paren = attribute_paren(code, j)
        if paren is not None and (close := pairs.get(paren)) is not None:
            j = close + 1
            continue
        if code.startswith("[[", j) and (close := pairs.get(j)) is not None:
            j = close + 1
            continue
        break
    return j


def find_function_definitions(src: CSource, name: str, file: str = "") -> list[FunctionDefinition]:
    code = src.code_nodirectives
    pairs = src.pairs_nodirectives
    depth_at = _brace_depth_finder(code)
    out: list[FunctionDefinition] = []
    for m in re.finditer(rf"(?<![\w.])(?<!->)\b{re.escape(name)}\s*\(", code):
        if depth_at(m.start()) > 0:
            continue
        close = pairs.get(m.end() - 1)
        if close is None:
            continue
        j = _skip_attributes(code, pairs, close + 1, _target_attribute_paren)
        if j < len(code) and code[j] == "{":
            end = pairs.get(j)
            body = code[j + 1 : end if end is not None else len(code)]
            out.append(FunctionDefinition(file, src.line_of(m.start()), body, is_trivial_body(body)))
    return out


def function_definition_sites(src: CSource) -> list[DefinitionSite]:
    code = src.code_nodirectives
    pairs = src.pairs_nodirectives
    depth_at = _brace_depth_finder(code)
    out: list[DefinitionSite] = []
    for m in _DEF_CANDIDATE_RE.finditer(code):
        name = m.group(1)
        if name in NOT_A_FUNCTION:
            continue
        if depth_at(m.start()) > 0:
            continue
        close = pairs.get(m.end() - 1)
        if close is None:
            continue
        k = _skip_attributes(code, pairs, close + 1, _any_attribute_paren)
        if k < len(code) and code[k] == "{":
            out.append(DefinitionSite(name, m.start(), k))
    return out


def defined_functions(src: CSource) -> list[tuple[str, int]]:
    return [(site.name, site.offset) for site in function_definition_sites(src)]
