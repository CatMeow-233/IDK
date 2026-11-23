from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

from leanc.lang.c_lexer import CSource
from leanc.rules.definitions import function_definition_sites
from leanc.rules.loop_contracts import LoopContract
from leanc.rules.preprocessor import linemarker

__all__ = [
    "CONTRACTABLE_KEYWORDS",
    "FUNCTION_PROBE_PREFIX",
    "LOOP_KEYWORDS",
    "LOOP_PROBE_PREFIX",
    "FunctionEntries",
    "FunctionEntry",
    "Insertion",
    "LoopSite",
    "LoopSiteMatch",
    "MatchedLoop",
    "Origin",
    "SiteProblem",
    "SiteProblemKind",
    "annotate",
    "contract_insertion",
    "function_entries",
    "function_probe_insertion",
    "function_probe_label",
    "loop_probe_insertion",
    "loop_probe_label",
    "loop_sites",
    "origin_lines",
    "probe_statement",
]

LOOP_KEYWORDS = ("for", "while", "do", "goto")
CONTRACTABLE_KEYWORDS = frozenset({"for", "while"})
FUNCTION_PROBE_PREFIX = "leanc_function:"
LOOP_PROBE_PREFIX = "leanc_loop:"
HEADER_OPEN = "("
BODY_OPEN = "{"
EMPTY_STATEMENT = ";"
LINE_BREAKS = ("\n", "\r")
LABEL_ENCODING = "utf-8"
LABEL_ERRORS = "surrogateescape"
_LOOP_KEYWORD_RE = re.compile(rf"\b(?:{'|'.join(LOOP_KEYWORDS)})\b")
_NON_SPACE_RE = re.compile(r"\S")
_PRINTABLE_ASCII = range(0x20, 0x7F)
_LITERAL_ESCAPES = {'"': '\\"', "\\": "\\\\", "?": "\\?"}


@dataclass(frozen=True)
class Origin:
    file: str
    line: int


def origin_lines(pp: str) -> list[Origin | None]:
    origins: list[Origin | None] = []
    current: str | None = None
    line = 0
    for text in pp.split("\n"):
        marker = linemarker(text)
        if marker is not None:
            line, current = marker.line, posixpath.normpath(marker.file)
            origins.append(None)
            continue
        origins.append(None if current is None else Origin(current, line))
        line += 1
    return origins


class SiteProblemKind(str, Enum):
    UNANALYZABLE = "unanalyzable"
    NO_LOOP = "no_loop"
    SEVERAL_LOOPS = "several_loops"
    NOT_CONTRACTABLE = "not_contractable"
    MACRO_LOOP = "macro_loop"
    NO_HEADER = "no_header"
    HEADER_OUTSIDE_FILE = "header_outside_file"
    EMPTY_BODY = "empty_body"
    BODY_NOT_BLOCK = "body_not_block"
    DIGRAPH_BRACE = "digraph_brace"


@dataclass(frozen=True)
class SiteProblem:
    kind: SiteProblemKind
    file: str
    line: int | None
    message: str

    @property
    def location(self) -> str:
        return self.file if self.line is None else f"{self.file}:{self.line}"

    def describe(self) -> str:
        return f"{self.location}: {self.message}"


@dataclass(frozen=True)
class LoopSite:
    file: str
    line: int
    keyword: str
    header_end_line: int
    header_close: int
    body_open: int

    @property
    def clause_offset(self) -> int:
        return self.header_close + 1

    @property
    def body_offset(self) -> int:
        return self.body_open + 1


@dataclass(frozen=True)
class MatchedLoop:
    contract: LoopContract
    site: LoopSite


@dataclass(frozen=True)
class LoopSiteMatch:
    matched: tuple[MatchedLoop, ...] = ()
    problems: tuple[SiteProblem, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class FunctionEntry:
    name: str
    file: str
    line: int
    body_open: int

    @property
    def body_offset(self) -> int:
        return self.body_open + 1


@dataclass(frozen=True)
class FunctionEntries:
    entries: tuple[FunctionEntry, ...] = ()
    problems: tuple[SiteProblem, ...] = ()


@dataclass(frozen=True)
class Insertion:
    offset: int
    text: str


@dataclass(frozen=True)
class _Keyword:
    word: str
    offset: int


class _Preprocessed:
    def __init__(self, rel: str, pp: str) -> None:
        self.rel = rel
        self.text = pp
        self.source = CSource(pp)
        self.code = self.source.code_nodirectives
        self.origins = origin_lines(pp)

    @property
    def spliced(self) -> bool:
        return self.source.text != self.text

    def origin_at(self, offset: int) -> Origin | None:
        return self.origins[self.source.line_of(offset) - 1]

    def in_file(self, offset: int) -> Origin | None:
        origin = self.origin_at(offset)
        return origin if origin is not None and origin.file == self.rel else None

    def keywords_by_line(self) -> dict[int, list[_Keyword]]:
        found: dict[int, list[_Keyword]] = {}
        for keyword in _LOOP_KEYWORD_RE.finditer(self.code):
            origin = self.in_file(keyword.start())
            if origin is not None:
                found.setdefault(origin.line, []).append(_Keyword(keyword.group(), keyword.start()))
        return found

    def next_token(self, offset: int) -> int | None:
        token = _NON_SPACE_RE.search(self.code, offset)
        return token.start() if token else None


def _spelled_keywords(source: str) -> dict[int, list[str]]:
    src = CSource(source)
    found: dict[int, list[str]] = {}
    for keyword in _LOOP_KEYWORD_RE.finditer(src.code_nodirectives):
        found.setdefault(src.line_of(keyword.start()), []).append(keyword.group())
    return found


def _problem(kind: SiteProblemKind, contract: LoopContract, message: str) -> SiteProblem:
    return SiteProblem(kind, contract.file, contract.line, message)


def _unanalyzable(rel: str) -> str:
    return f"the cbmc --preprocess output of {rel} still has a line splice, so offsets cannot be mapped"


def _site_after_header(view: _Preprocessed, keyword: _Keyword, contract: LoopContract) -> LoopSite | SiteProblem:
    opening = view.next_token(keyword.offset + len(keyword.word))
    close = view.source.pairs_nodirectives.get(opening) if opening is not None else None
    if opening is None or view.code[opening] != HEADER_OPEN or close is None:
        return _problem(SiteProblemKind.NO_HEADER, contract, f"`{keyword.word}` is not followed by a ( ) header")
    header_end = view.in_file(close)
    if header_end is None:
        message = f"the header of this {keyword.word} loop ends outside {view.rel}"
        return _problem(SiteProblemKind.HEADER_OUTSIDE_FILE, contract, message)
    body = view.next_token(close + 1)
    if body is not None and view.code[body] == EMPTY_STATEMENT:
        message = f"`;` follows the {keyword.word} header (the while of a do-while loop, or a loop without a body)"
        return _problem(SiteProblemKind.EMPTY_BODY, contract, message)
    if body is None or view.code[body] != BODY_OPEN:
        message = f"the body of this {keyword.word} loop is not a {{ }} block; put the body in braces"
        return _problem(SiteProblemKind.BODY_NOT_BLOCK, contract, message)
    if view.text[body] != BODY_OPEN:
        message = f"the body of this {keyword.word} loop opens with the digraph <%; write {{"
        return _problem(SiteProblemKind.DIGRAPH_BRACE, contract, message)
    return LoopSite(view.rel, contract.line, keyword.word, header_end.line, close, body)


def _site(
    view: _Preprocessed, found: list[_Keyword], spelled: list[str], contract: LoopContract
) -> LoopSite | SiteProblem:
    if not found:
        return _problem(SiteProblemKind.NO_LOOP, contract, "no for or while loop starts on this line")
    if len(found) > 1:
        words = ", ".join(keyword.word for keyword in found)
        message = f"{len(found)} loop keywords on this line ({words}); put each loop on its own line"
        return _problem(SiteProblemKind.SEVERAL_LOOPS, contract, message)
    keyword = found[0]
    if keyword.word not in CONTRACTABLE_KEYWORDS:
        message = f"a {keyword.word} loop cannot take a contract; only for and while loops can"
        return _problem(SiteProblemKind.NOT_CONTRACTABLE, contract, message)
    if spelled != [keyword.word]:
        message = f"the {keyword.word} on this line comes from a macro expansion; spell the loop out in the source"
        return _problem(SiteProblemKind.MACRO_LOOP, contract, message)
    return _site_after_header(view, keyword, contract)


def loop_sites(rel: str, pp: str, source: str, contracts: Sequence[LoopContract]) -> LoopSiteMatch:
    rel = posixpath.normpath(rel)
    wanted = [contract for contract in contracts if posixpath.normpath(contract.file) == rel]
    if not wanted:
        return LoopSiteMatch()
    view = _Preprocessed(rel, pp)
    if view.spliced:
        return LoopSiteMatch(
            problems=tuple(_problem(SiteProblemKind.UNANALYZABLE, c, _unanalyzable(rel)) for c in wanted)
        )
    keywords = view.keywords_by_line()
    spelled = _spelled_keywords(source)
    matched: list[MatchedLoop] = []
    problems: list[SiteProblem] = []
    for contract in wanted:
        outcome = _site(view, keywords.get(contract.line, []), spelled.get(contract.line, []), contract)
        if isinstance(outcome, SiteProblem):
            problems.append(outcome)
        else:
            matched.append(MatchedLoop(contract, outcome))
    return LoopSiteMatch(tuple(matched), tuple(problems))


def function_entries(rel: str, pp: str, names: Iterable[str]) -> FunctionEntries:
    rel = posixpath.normpath(rel)
    wanted = frozenset(names)
    view = _Preprocessed(rel, pp)
    if view.spliced:
        return FunctionEntries(problems=(SiteProblem(SiteProblemKind.UNANALYZABLE, rel, None, _unanalyzable(rel)),))
    entries: list[FunctionEntry] = []
    problems: list[SiteProblem] = []
    for definition in function_definition_sites(view.source):
        origin = view.in_file(definition.offset)
        if definition.name not in wanted or origin is None:
            continue
        if view.text[definition.body_open] != BODY_OPEN:
            message = f"the body of {definition.name} opens with the digraph <%; write {{"
            problems.append(SiteProblem(SiteProblemKind.DIGRAPH_BRACE, rel, origin.line, message))
            continue
        entries.append(FunctionEntry(definition.name, rel, origin.line, definition.body_open))
    return FunctionEntries(tuple(entries), tuple(problems))


def function_probe_label(name: str) -> str:
    return f"{FUNCTION_PROBE_PREFIX}{name}"


def loop_probe_label(file: str, line: int) -> str:
    return f"{LOOP_PROBE_PREFIX}{file}:{line}"


def _c_string_literal(text: str) -> str:
    characters = []
    for byte in text.encode(LABEL_ENCODING, LABEL_ERRORS):
        character = chr(byte)
        if character in _LITERAL_ESCAPES:
            characters.append(_LITERAL_ESCAPES[character])
        elif byte in _PRINTABLE_ASCII:
            characters.append(character)
        else:
            characters.append(f"\\{byte:03o}")
    return '"' + "".join(characters) + '"'


def probe_statement(label: str) -> str:
    return f" __CPROVER_assert(0, {_c_string_literal(label)}); "


def contract_insertion(matched: MatchedLoop) -> Insertion:
    return Insertion(matched.site.clause_offset, matched.contract.annotation())


def loop_probe_insertion(matched: MatchedLoop) -> Insertion:
    site = matched.site
    return Insertion(site.body_offset, probe_statement(loop_probe_label(site.file, site.line)))


def function_probe_insertion(entry: FunctionEntry) -> Insertion:
    return Insertion(entry.body_offset, probe_statement(function_probe_label(entry.name)))


def _checked(pp: str, insertions: Iterable[Insertion]) -> list[Insertion]:
    ordered = sorted(insertions, key=lambda insertion: insertion.offset, reverse=True)
    seen: set[int] = set()
    for insertion in ordered:
        if not 0 <= insertion.offset <= len(pp):
            raise ValueError(f"insertion offset {insertion.offset} is outside the text (0..{len(pp)})")
        if insertion.offset in seen:
            raise ValueError(f"two insertions at offset {insertion.offset}")
        if any(line_break in insertion.text for line_break in LINE_BREAKS):
            raise ValueError(f"insertion at offset {insertion.offset} contains a line break")
        seen.add(insertion.offset)
    return ordered


def annotate(pp: str, insertions: Iterable[Insertion]) -> str:
    pieces: list[str] = []
    end = len(pp)
    for insertion in _checked(pp, insertions):
        pieces += [pp[insertion.offset : end], insertion.text]
        end = insertion.offset
    pieces.append(pp[:end])
    return "".join(reversed(pieces))
