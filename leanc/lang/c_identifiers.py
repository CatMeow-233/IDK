from __future__ import annotations

import re

__all__ = ["C_KEYWORDS", "HARNESS_INPUT_PREFIX", "RESERVED_IDENTIFIER_RE"]

C_KEYWORDS: frozenset[str] = frozenset(
    {
        "auto", "break", "case", "char", "const", "continue", "default", "do", "double", "else", "enum",
        "extern", "float", "for", "goto", "if", "inline", "int", "long", "register", "restrict", "return",
        "short", "signed", "sizeof", "static", "struct", "switch", "typedef", "union", "unsigned", "void",
        "volatile", "while", "alignas", "alignof", "typeof", "typeof_unqual", "constexpr", "nullptr",
        "thread_local",
    }
)

RESERVED_IDENTIFIER_RE = re.compile(r"^(?:__\w*|_[A-Z]\w*)$")

HARNESS_INPUT_PREFIX = "nondet_"
