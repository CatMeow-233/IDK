from __future__ import annotations

import re

from leanc.foundation.errors import PatchError, WorkspaceError
from leanc.foundation.workspace import normalize_relpath

DEV_NULL = "/dev/null"
GIT_OLD_PREFIX = "a/"
GIT_NEW_PREFIX = "b/"
HEADER_TIMESTAMP_SEPARATOR = "\t"

_QUOTE = '"'
_BACKSLASH = "\\"
_GIT_SIMPLE_ESCAPES = {
    "n": b"\n",
    "t": b"\t",
    "r": b"\r",
    '"': b'"',
    "\\": b"\\",
    "a": b"\a",
    "b": b"\b",
    "f": b"\f",
    "v": b"\v",
}
_OCTAL_ESCAPE_DIGITS = 3
_OCTAL_ESCAPE_RE = re.compile(r"[0-7]{3}")
_BYTE_MASK = 0xFF
_CHARACTERS_GIT_QUOTES = '\t\n\r"\\'
_FIRST_PRINTABLE_CODEPOINT = 32
_GIT_HEADER_PAIR_RE = re.compile(r'^("(?:[^"\\]|\\.)*"|\S+) ("(?:[^"\\]|\\.)*"|\S+)$')


def _is_git_quoted(spelling: str) -> bool:
    return len(spelling) >= 2 and spelling.startswith(_QUOTE) and spelling.endswith(_QUOTE)


def unquote_git_path(spelling: str) -> str:
    if not _is_git_quoted(spelling):
        return spelling
    body = spelling[1:-1]
    decoded = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        if char == _BACKSLASH and index + 1 < len(body):
            escaped = body[index + 1]
            if escaped in _GIT_SIMPLE_ESCAPES:
                decoded += _GIT_SIMPLE_ESCAPES[escaped]
                index += 2
                continue
            octal = body[index + 1 : index + 1 + _OCTAL_ESCAPE_DIGITS]
            if _OCTAL_ESCAPE_RE.match(octal):
                decoded.append(int(octal, 8) & _BYTE_MASK)
                index += 1 + _OCTAL_ESCAPE_DIGITS
                continue
        decoded += char.encode("utf-8")
        index += 1
    return decoded.decode("utf-8", errors="replace")


def _needs_git_quoting(path: str) -> bool:
    return any(c in path for c in _CHARACTERS_GIT_QUOTES) or any(ord(c) < _FIRST_PRINTABLE_CODEPOINT for c in path)


def quote_path(path: str) -> str:
    if not _needs_git_quoting(path):
        return path
    escaped = (
        path.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
    )
    return f'"{escaped}"'


def header_path(raw: str) -> str | None:
    spelling = raw.rstrip("\r\n")
    if not spelling.startswith(_QUOTE) and HEADER_TIMESTAMP_SEPARATOR in spelling:
        spelling = spelling.split(HEADER_TIMESTAMP_SEPARATOR, 1)[0]
    spelling = unquote_git_path(spelling.strip())
    if spelling == DEV_NULL:
        return None
    return spelling


def strip_git_prefixes(old: str | None, new: str | None) -> tuple[str | None, str | None]:
    old_prefixed = old is None or old.startswith(GIT_OLD_PREFIX)
    new_prefixed = new is None or new.startswith(GIT_NEW_PREFIX)
    if old_prefixed and new_prefixed:
        return (
            None if old is None else old[len(GIT_OLD_PREFIX) :],
            None if new is None else new[len(GIT_NEW_PREFIX) :],
        )
    return old, new


def safe_path(path: str | None, lineno: int) -> str | None:
    if path is None:
        return None
    try:
        return normalize_relpath(path)
    except WorkspaceError as exc:
        raise PatchError(f"line {lineno}: unsafe path in patch: {exc}") from None


def _identical_unquoted_names(rest: str) -> tuple[str, str] | None:
    separator = " " + GIT_NEW_PREFIX
    name_chars = len(rest) - len(GIT_OLD_PREFIX) - len(separator)
    if not rest.startswith(GIT_OLD_PREFIX) or name_chars % 2:
        return None
    name = rest[len(GIT_OLD_PREFIX) : len(GIT_OLD_PREFIX) + name_chars // 2]
    if rest != f"{GIT_OLD_PREFIX}{name}{separator}{name}":
        return None
    return f"{GIT_OLD_PREFIX}{name}", f"{GIT_NEW_PREFIX}{name}"


def git_header_paths(rest: str) -> tuple[str | None, str | None]:
    rest = rest.rstrip("\r\n")
    pair = _GIT_HEADER_PAIR_RE.match(rest)
    if pair:
        return unquote_git_path(pair.group(1)), unquote_git_path(pair.group(2))
    same_name = _identical_unquoted_names(rest)
    if same_name is not None:
        return same_name
    return None, None
