from __future__ import annotations

import re
from typing import Any

INTERNAL_NAME_PREFIXES = (
    "return_value_",
    "__CPROVER",
    "tmp_",
    "__VERIFIER",
    "goto_symex",
    "symex",
    "__builtin",
)
RESERVED_IDENTIFIER_PREFIX = "__"
INTERNAL_NAME_MARKERS = ("$", "#")
INTERNAL_FUNCTION_PREFIXES = ("__CPROVER", "__VERIFIER")
SCOPE_SEPARATOR = "::"
CPROVER_TYPE_ALIASES = (
    ("__CPROVER_size_t", "size_t"),
    ("__CPROVER_ssize_t", "ssize_t"),
    ("__CPROVER_bool", "bool"),
)

_DECORATION_RE = re.compile(r"(?:![0-9]+@[0-9]+|#[0-9]+|\$object)")
_SUFFIXED_INDEX_RE = re.compile(r"\[(-?\d+)[uUlL]*\]")
_BASE_NAME_END_RE = re.compile(r"[\[.]")


def strip_decorations(text: str) -> str:
    return _DECORATION_RE.sub("", text)


def clean_name(name: str | None) -> str:
    if not name:
        return ""
    cleaned = strip_decorations(str(name))
    if SCOPE_SEPARATOR in cleaned and "[" not in cleaned and "." not in cleaned:
        cleaned = cleaned.split(SCOPE_SEPARATOR)[-1]
    return _SUFFIXED_INDEX_RE.sub(r"[\1]", cleaned)


def is_internal_name(name: str | None) -> bool:
    if not name:
        return True
    text = str(name)
    if any(marker in text for marker in INTERNAL_NAME_MARKERS):
        return True
    base = _BASE_NAME_END_RE.split(text, maxsplit=1)[0].split(SCOPE_SEPARATOR)[-1]
    return not base or base.startswith((*INTERNAL_NAME_PREFIXES, RESERVED_IDENTIFIER_PREFIX))


def is_internal_function(function_id: str | None) -> bool:
    return not function_id or function_id.startswith(INTERNAL_FUNCTION_PREFIXES)


def clean_type(type_name: Any) -> str | None:
    if not isinstance(type_name, str) or not type_name:
        return None
    cleaned = type_name
    for cprover_name, c_name in CPROVER_TYPE_ALIASES:
        cleaned = cleaned.replace(cprover_name, c_name)
    return cleaned
