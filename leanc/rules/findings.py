from __future__ import annotations

import re
from typing import NamedTuple

from leanc.foundation.models import PolicyViolation
from leanc.lang.c_lexer import CSource

__all__ = ["Finding", "Problem", "dedupe_key", "first_mention_line", "to_violations", "unique_id", "violation"]


class Finding(NamedTuple):
    rule: str
    message: str
    offset: int


class Problem(NamedTuple):
    rule: str
    message: str


def violation(rule: str, message: str, rel: str | None, line: int | None) -> PolicyViolation:
    return PolicyViolation(rule=rule, message=message, file=rel, line=line)


def dedupe_key(found: PolicyViolation) -> tuple:
    return (found.rule, found.file, found.line) if found.line else (found.rule, found.file, found.message)


def unique_id(base: str, taken: set[str]) -> str:
    candidate, suffix = base, 2
    while candidate in taken:
        candidate, suffix = f"{base}#{suffix}", suffix + 1
    taken.add(candidate)
    return candidate


def _reported_line(src: CSource, offset: int, line_map: list[int] | None) -> int:
    line = src.line_of(offset)
    if line_map is None:
        return line
    return line_map[line - 1] if 0 < line <= len(line_map) else 0


def to_violations(
    src: CSource, rel: str, found: list[Finding], line_map: list[int] | None = None
) -> list[PolicyViolation]:
    out: list[PolicyViolation] = []
    seen: set[tuple[str, int]] = set()
    for finding in sorted(found, key=lambda f: (f.offset, f.rule)):
        line = _reported_line(src, finding.offset, line_map)
        if (finding.rule, line) in seen:
            continue
        seen.add((finding.rule, line))
        shown = src.shown_line(src.line_of(finding.offset))
        message = f"{finding.message}: `{shown}`" if shown else finding.message
        out.append(violation(finding.rule, message, rel, line or None))
    return out


def first_mention_line(src: CSource | None, name: str) -> int | None:
    if src is None:
        return None
    mention = re.search(rf"\b{re.escape(name)}\b", src.code)
    return src.line_of(mention.start()) if mention else None
