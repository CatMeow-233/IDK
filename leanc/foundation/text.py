from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")
NUL = "\x00"
SAFE_NAME_MAX_LENGTH = 80
SAFE_NAME_EXTRA_CHARS = "-_."
SAFE_NAME_REPLACEMENT = "_"


def _escaped_code_point(match: re.Match[str]) -> str:
    return f"\\u{ord(match.group()):04x}"


def utf8_safe(text: str) -> str:
    return LONE_SURROGATE_RE.sub(_escaped_code_point, text)


def has_lone_surrogate(text: str) -> bool:
    return LONE_SURROGATE_RE.search(text) is not None


def lone_surrogate_paths(value: Any, path: str = "$") -> Iterator[str]:
    if isinstance(value, str):
        if has_lone_surrogate(value):
            yield path
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and has_lone_surrogate(key):
                yield f"{path} key {utf8_safe(key)!r}"
            yield from lone_surrogate_paths(item, f"{path}.{utf8_safe(str(key))}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from lone_surrogate_paths(item, f"{path}[{index}]")


def _safe_char(char: str) -> str:
    return char if (char.isascii() and char.isalnum()) or char in SAFE_NAME_EXTRA_CHARS else SAFE_NAME_REPLACEMENT


def safe_name(text: str, max_length: int = SAFE_NAME_MAX_LENGTH) -> str:
    return "".join(_safe_char(char) for char in text)[:max_length]
