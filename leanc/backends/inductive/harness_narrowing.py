from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

__all__ = [
    "EARLY_EXIT_RULE",
    "PRE_CALL_WRITE_RULE",
    "Span",
    "main_exit_problems",
    "pre_call_write_problems",
]

Where = Callable[[int], str]

BLOCK_OPEN = "{"
STATEMENT_END = ";"
STATEMENT_BOUNDARIES = frozenset({";", "{", "}"})
DECLARATOR_ASSIGNMENT = "="
POINTER_TOKEN = "*"
MIN_DECLARATION_WORDS = 2
ENTRY_FUNCTION = "main()"
EARLY_EXIT_RULE = "skips the harness assertions on some paths like an assumption"
PRE_CALL_WRITE_RULE = "narrows the input of the claimed call without being listed"
_RETURN_RE = re.compile(r"(?<![\w$])return(?![\w$])")
_GOTO_RE = re.compile(r"(?<![\w$])goto(?![\w$])")
_OPERATOR_RE = re.compile(r"<<=|>>=|\+\+|--|->|==|!=|<=|>=|[+\-*/%&|^]=|=")
_WRITE_OPERATORS = frozenset({"<<=", ">>=", "++", "--", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "="})
_DECLARATION_TOKEN_RE = re.compile(r"[A-Za-z_]\w*|\*|\[[^\[\]]*\]|\S")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
_STATEMENT_KEYWORDS = frozenset(
    {"return", "goto", "case", "default", "sizeof", "else", "do", "if", "while", "for", "switch", "break", "continue"}
)


@dataclass(frozen=True)
class Span:
    open: int
    close: int


def _innermost_open(pairs: Mapping[int, int], within: Span, offset: int) -> int:
    enclosing = [
        opening for opening, closing in pairs.items() if within.open <= opening < offset < closing <= within.close
    ]
    return max(enclosing, default=within.open)


def _statement_start(code: str, pairs: Mapping[int, int], block_open: int, offset: int) -> int:
    start = block_open + 1
    position = start
    while position < offset:
        closing = pairs.get(position)
        if closing is not None and closing < offset:
            position = closing + 1
            if code[closing] == "}":
                start = position
            continue
        if code[position] == STATEMENT_END:
            start = position + 1
        position += 1
    return start


def _statement_end(code: str, pairs: Mapping[int, int], offset: int, limit: int) -> int | None:
    position = offset
    while position < limit:
        if code[position] == STATEMENT_END:
            return position
        closing = pairs.get(position)
        position = closing + 1 if closing is not None else position + 1
    return None


def _is_declaration_head(text: str) -> bool:
    tokens = _DECLARATION_TOKEN_RE.findall(text)
    while tokens and tokens[-1].startswith("["):
        tokens.pop()
    if not tokens or not _IDENTIFIER_RE.fullmatch(tokens[0]) or tokens[0] in _STATEMENT_KEYWORDS:
        return False
    if not _IDENTIFIER_RE.fullmatch(tokens[-1]):
        return False
    if any(token != POINTER_TOKEN and not _IDENTIFIER_RE.fullmatch(token) for token in tokens):
        return False
    return sum(1 for token in tokens if token != POINTER_TOKEN) >= MIN_DECLARATION_WORDS


def _is_declarator_assignment(code: str, pairs: Mapping[int, int], main: Span, offset: int, operator: str) -> bool:
    if operator != DECLARATOR_ASSIGNMENT:
        return False
    block_open = _innermost_open(pairs, main, offset)
    if code[block_open] != BLOCK_OPEN:
        return False
    return _is_declaration_head(code[_statement_start(code, pairs, block_open, offset) : offset])


def pre_call_write_problems(
    code: str, pairs: Mapping[int, int], main: Span, window: Span, function: str, call_where: str, where: Where
) -> list[str]:
    problems = []
    for match in _OPERATOR_RE.finditer(code, window.open, window.close):
        operator = match.group()
        if operator not in _WRITE_OPERATORS or _is_declarator_assignment(code, pairs, main, match.start(), operator):
            continue
        problems.append(
            f"{where(match.start())}: {operator} writes in {ENTRY_FUNCTION} after its last LEANC_ASSUME (or its "
            f"start) and up to the end of the call of the claimed function {function} at {call_where}; it "
            f"{PRE_CALL_WRITE_RULE} (only the = of a plain declaration T x = ...; may appear there)"
        )
    return problems


def _is_final_return(code: str, pairs: Mapping[int, int], main: Span, offset: int) -> bool:
    if _innermost_open(pairs, main, offset) != main.open:
        return False
    before = code[main.open + 1 : offset].rstrip()
    if before and before[-1] not in STATEMENT_BOUNDARIES:
        return False
    end = _statement_end(code, pairs, offset, main.close)
    return end is not None and code[end + 1 : main.close].strip() == ""


def main_exit_problems(code: str, pairs: Mapping[int, int], main: Span, where: Where) -> list[str]:
    problems = [
        f"{where(match.start())}: return before the end of {ENTRY_FUNCTION} {EARLY_EXIT_RULE}; only one final "
        f"top-level return of {ENTRY_FUNCTION} is allowed"
        for match in _RETURN_RE.finditer(code, main.open + 1, main.close)
        if not _is_final_return(code, pairs, main, match.start())
    ]
    problems += [
        f"{where(match.start())}: goto in {ENTRY_FUNCTION} {EARLY_EXIT_RULE}"
        for match in _GOTO_RE.finditer(code, main.open + 1, main.close)
    ]
    return problems
