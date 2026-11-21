from __future__ import annotations

import re

QUOTES = "\"'"
ESCAPE = "\\"
OPENING_CONDITIONALS = frozenset({"if", "ifdef", "ifndef"})
LINE_CONTINUATION = "\\"

_REACH_USE_RE = re.compile(r"\bLEANC_REACH\s*\(")
_REACH_LABEL_RE = re.compile(r'\bLEANC_REACH\s*\(\s*"((?:[^"\\\n]|\\.)*)"\s*\)')
_CONDITIONAL_RE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b")
_DIRECTIVE_RE = re.compile(r"^\s*#")
_MAIN_DEF_RE = re.compile(r"\bmain\s*\([^;{})]*\)\s*\{")
_ASSERT_USE_RE = re.compile(r"\b(?:__CPROVER_assert|assert)\s*\(")


def _literal_end(code: str, start: int) -> int:
    quote = code[start]
    position = start + 1
    while position < len(code) and code[position] != quote and code[position] != "\n":
        position += 2 if code[position] == ESCAPE else 1
    return min(position + 1, len(code))


def _line_comment_end(code: str, start: int) -> int:
    newline = code.find("\n", start)
    return len(code) if newline < 0 else newline


def _block_comment_end(code: str, start: int) -> int:
    close = code.find("*/", start + 2)
    return len(code) if close < 0 else close + 2


def _blank_keeping_newlines(text: str) -> str:
    return "".join(char if char == "\n" else " " for char in text)


def strip_c_comments(code: str) -> str:
    pieces: list[str] = []
    position = 0
    while position < len(code):
        char = code[position]
        following = code[position + 1] if position + 1 < len(code) else ""
        if char == "/" and following == "/":
            end = _line_comment_end(code, position)
            pieces.append(" " * (end - position))
        elif char == "/" and following == "*":
            end = _block_comment_end(code, position)
            pieces.append(_blank_keeping_newlines(code[position:end]))
        elif char in QUOTES:
            end = _literal_end(code, position)
            pieces.append(code[position:end])
        else:
            end = position + 1
            pieces.append(char)
        position = end
    return "".join(pieces)


def find_probes(harness_text: str) -> tuple[bool, list[str]]:
    code = strip_c_comments(harness_text)
    if not _REACH_USE_RE.search(code):
        return False, []
    labels = list(dict.fromkeys(match.group(1) for match in _REACH_LABEL_RE.finditer(code)))
    return True, labels


def probe_occurrences(harness_text: str) -> tuple[int, dict[str, int]]:
    code = strip_c_comments(harness_text)
    counts: dict[str, int] = {}
    for match in _REACH_LABEL_RE.finditer(code):
        counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    return len(_REACH_USE_RE.findall(code)), counts


def _blank_literal_contents(literal: str) -> str:
    closing = literal[-1] if len(literal) >= 2 else ""
    return literal[0] + " " * max(0, len(literal) - 2) + closing


def _blank_string_literals(code: str) -> str:
    pieces: list[str] = []
    position = 0
    while position < len(code):
        if code[position] in QUOTES:
            end = _literal_end(code, position)
            pieces.append(_blank_literal_contents(code[position:end]))
        else:
            end = position + 1
            pieces.append(code[position])
        position = end
    return "".join(pieces)


def _continues_on_next_line(line: str) -> bool:
    return line.rstrip().endswith(LINE_CONTINUATION)


def _blank_directives_and_conditional_blocks(code: str) -> str:
    kept: list[str] = []
    conditional_depth = 0
    continued = False
    for line in code.split("\n"):
        if continued:
            continued = _continues_on_next_line(line)
            kept.append("")
            continue
        if _DIRECTIVE_RE.match(line):
            conditional = _CONDITIONAL_RE.match(line)
            if conditional and conditional.group(1) in OPENING_CONDITIONALS:
                conditional_depth += 1
            elif conditional and conditional.group(1) == "endif":
                conditional_depth = max(0, conditional_depth - 1)
            continued = _continues_on_next_line(line)
            kept.append("")
            continue
        kept.append("" if conditional_depth > 0 else line)
    return "\n".join(kept)


def _braced_body(code: str, opening_brace: int) -> str:
    depth = 0
    for position in range(opening_brace, len(code)):
        if code[position] == "{":
            depth += 1
        elif code[position] == "}":
            depth -= 1
            if depth == 0:
                return code[opening_brace : position + 1]
    return code[opening_brace:]


def count_harness_assertions(harness_text: str) -> int:
    code = _blank_directives_and_conditional_blocks(_blank_string_literals(strip_c_comments(harness_text)))
    main_definition = _MAIN_DEF_RE.search(code)
    if not main_definition:
        return 0
    body = _braced_body(code, main_definition.end() - 1)
    return len(_ASSERT_USE_RE.findall(body))
