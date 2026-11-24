from __future__ import annotations

import re
from dataclasses import dataclass

from leanc.backends.counterexample.properties import REACH_PREFIX
from leanc.lang.c_lexer import CSource
from leanc.rules.loop_sites import Origin, origin_lines

__all__ = [
    "DIVERGENCE_RULE",
    "DIVERGENCE_RULE_ID",
    "REACH_VACUITY_EXPANSION",
    "REACH_VERIFY_EXPANSION",
    "Divergence",
    "ReachSite",
    "normalised_vacuity_text",
    "reach_sites",
    "vacuity_divergence",
]

REACH_VERIFY_EXPANSION = "((void)0)"
REACH_VACUITY_EXPANSION = f'__CPROVER_assert(0, "{REACH_PREFIX}" label)'
DIVERGENCE_RULE = (
    "preprocesses to different programs with and without LEANC_VACUITY_CHECK outside its LEANC_REACH probes, so "
    "the vacuity run would show the reachability of another program than the one verified"
)
DIVERGENCE_RULE_ID = "vacuity-divergence"
MAX_SHOWN_LINE_CHARS = 160
END_OF_OUTPUT = "<end of the preprocessed text>"
MEMBER_ACCESS_ENDINGS = (".", "->")
UNKNOWN_ORIGIN = "?"
_BLANK = r"[ \t]*"
_LITERAL = r'"(?:[^"\\\n]|\\.)*"'
_LITERAL_RE = re.compile(_LITERAL)
_SITE_RE = re.compile(
    rf'__CPROVER_assert{_BLANK}\({_BLANK}0{_BLANK},{_BLANK}"{re.escape(REACH_PREFIX)}"(?:{_BLANK}{_LITERAL})+{_BLANK}\)'
)
_CODE_SHAPE_RE = re.compile(rf'__CPROVER_assert{_BLANK}\({_BLANK}0{_BLANK},{_BLANK}" *"(?:{_BLANK}" *")+{_BLANK}\)')


@dataclass(frozen=True)
class Divergence:
    origin: Origin | None
    line: int
    verify_line: str
    vacuity_line: str

    @property
    def where(self) -> str:
        return f"{self.origin.file}:{self.origin.line}" if self.origin else f"preprocessed line {self.line}"

    def describe(self) -> str:
        return f"{self.where}: without the define {self.verify_line!r}, with it {self.vacuity_line!r}"


@dataclass(frozen=True)
class ReachSite:
    origin: Origin | None
    label: str

    @property
    def where(self) -> str:
        return f"{self.origin.file}:{self.origin.line}" if self.origin else UNKNOWN_ORIGIN

    def describe(self) -> str:
        return f'LEANC_REACH("{self.label}") at {self.where}'


def _is_member_access(code: str, start: int) -> bool:
    position = start
    while position > 0 and code[position - 1].isspace():
        position -= 1
    return code.endswith(MEMBER_ACCESS_ENDINGS, 0, position)


def _site_matches(text: str) -> list[re.Match[str]] | None:
    code = CSource(text).code
    if len(code) != len(text):
        return None
    matches: list[re.Match[str]] = []
    position = 0
    while (match := _SITE_RE.search(text, position)) is not None:
        if _CODE_SHAPE_RE.fullmatch(code, match.start(), match.end()) is None or _is_member_access(code, match.start()):
            position = match.start() + 1
            continue
        matches.append(match)
        position = match.end()
    return matches


def normalised_vacuity_text(text: str) -> str:
    matches = _site_matches(text)
    if matches is None:
        return text
    pieces: list[str] = []
    last = 0
    for match in matches:
        pieces += [text[last : match.start()], REACH_VERIFY_EXPANSION]
        last = match.end()
    pieces.append(text[last:])
    return "".join(pieces)


def _label(site: str) -> str:
    literals = _LITERAL_RE.findall(site)
    return "".join(literal[1:-1] for literal in literals[1:])


def reach_sites(text: str) -> tuple[ReachSite, ...]:
    matches = _site_matches(text) or []
    origins = origin_lines(text)
    sites = []
    for match in matches:
        index = text.count("\n", 0, match.start())
        sites.append(ReachSite(origins[index] if index < len(origins) else None, _label(match.group())))
    return tuple(sites)


def _shown(lines: list[str], index: int) -> str:
    return " ".join(lines[index].split())[:MAX_SHOWN_LINE_CHARS] if index < len(lines) else END_OF_OUTPUT


def vacuity_divergence(verify_text: str, vacuity_text: str) -> Divergence | None:
    normalised = normalised_vacuity_text(vacuity_text)
    if normalised == verify_text:
        return None
    verify_lines, normalised_lines = verify_text.split("\n"), normalised.split("\n")
    pairs = zip(verify_lines, normalised_lines)
    index = next((i for i, (left, right) in enumerate(pairs) if left != right), min(len(verify_lines), len(normalised_lines)))
    origins = origin_lines(verify_text)
    origin = origins[index] if index < len(origins) else None
    return Divergence(origin, index + 1, _shown(verify_lines, index), _shown(vacuity_text.split("\n"), index))
