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


def _children(value: Any, path: str) -> list[tuple[Any, str]]:
    if isinstance(value, dict):
        return [(item, f"{path}.{utf8_safe(str(key))}") for key, item in value.items()]
    if isinstance(value, (list, tuple)):
        return [(item, f"{path}[{index}]") for index, item in enumerate(value)]
    return []


def _own_lone_surrogate_paths(value: Any, path: str) -> list[str]:
    if isinstance(value, str):
        return [path] if has_lone_surrogate(value) else []
    if isinstance(value, dict):
        return [f"{path} key {utf8_safe(k)!r}" for k in value if isinstance(k, str) and has_lone_surrogate(k)]
    return []


def lone_surrogate_paths(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    pending = [(value, path)]
    while pending:
        current, current_path = pending.pop()
        found += _own_lone_surrogate_paths(current, current_path)
        pending += _children(current, current_path)
    return found


def nesting_depth_exceeds(value: Any, limit: int) -> bool:
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > limit:
            return True
        pending += [(item, depth + 1) for item, _ in _children(current, "")]
    return False


def _safe_char(char: str) -> str:
    return char if (char.isascii() and char.isalnum()) or char in SAFE_NAME_EXTRA_CHARS else SAFE_NAME_REPLACEMENT


def safe_name(text: str, max_length: int = SAFE_NAME_MAX_LENGTH) -> str:
    return "".join(_safe_char(char) for char in text)[:max_length]
