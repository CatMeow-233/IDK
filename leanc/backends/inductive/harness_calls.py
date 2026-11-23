from __future__ import annotations

import bisect
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from leanc.lang.c_lexer import CSource
from leanc.rules.definitions import function_definition_sites
from leanc.rules.loop_sites import Origin, origin_lines
from leanc.rules.preprocessor import linemarker

__all__ = [
    "ASSUME_AFTER_CLAIMED_CALL_LABEL",
    "ASSUME_BUILTIN",
    "CLAIMED_CALLS_COUNTER",
    "CLAIMED_CALL_LABEL_PREFIX",
    "ENTRY_POINT",
    "RETURN_PROBE_PREFIX",
    "AssumeSite",
    "CallSite",
    "HarnessLayout",
    "ReturnKind",
    "claimed_call_label",
    "harness_layout",
    "instrumented_harness",
    "return_probe_label",
    "body_references",
]

CLAIMED_CALLS_COUNTER = "leanc_claimed_calls"
CLAIMED_CALL_LABEL_PREFIX = "leanc_claimed_call:"
ASSUME_AFTER_CLAIMED_CALL_LABEL = "leanc_assume_after_claimed_call"
RETURN_PROBE_PREFIX = "leanc_return:"
RETURNED_VALUE = "leanc_returned"
ASSUMED_VALUE = "leanc_assumed"
ASSUME_BUILTIN = "__CPROVER_assume"
ENTRY_POINT = "main"
CALL_OPEN = "("
ARGUMENT_SEPARATOR = ","
MEMBER_DOT = "."
MEMBER_ARROW = "->"
LINEMARKER_ESCAPES = {"\\": "\\\\", '"': '\\"'}
_NON_SPACE_RE = re.compile(r"\S")
_RECORD_HEAD_RE = re.compile(r"\b(?:struct|union)\b[\s\w]*$")
_RESERVED_IDENTIFIER_RE = re.compile(r"(?<![\w$])leanc_\w*", re.IGNORECASE)
BLOCK_OPEN = "{"


class ReturnKind(str, Enum):
    VALUE = "value"
    VOID = "void"


class Scope(str, Enum):
    BODY = "body"
    FILE = "file"
    RECORD = "record"
    UNKNOWN_BLOCK = "unknown_block"


@dataclass(frozen=True)
class CallSite:
    function: str
    start: int
    close: int
    origin: Origin | None

    @property
    def where(self) -> str:
        return f"{self.origin.file}:{self.origin.line}" if self.origin else "?"


@dataclass(frozen=True)
class AssumeSite:
    start: int
    open: int
    close: int
    origin: Origin | None


@dataclass(frozen=True)
class _Wrap:
    prefix_at: int
    prefix: str
    suffix_at: int
    suffix: str

    @property
    def span(self) -> int:
        return self.suffix_at - self.prefix_at


@dataclass(frozen=True)
class HarnessLayout:
    calls: tuple[CallSite, ...] = ()
    assumes: tuple[AssumeSite, ...] = ()
    main_body_open: int | None = None
    problems: tuple[str, ...] = ()

    def call_sequence(self) -> tuple[tuple[str, Origin | None], ...]:
        return tuple((call.function, call.origin) for call in self.calls)


@dataclass(frozen=True)
class _Body:
    open: int
    close: int


@dataclass(frozen=True)
class _Reference:
    name: str
    start: int
    end: int


class _View:
    def __init__(self, pp: str) -> None:
        self.source = CSource(pp)
        self.code = self.source.code_nodirectives
        self.pairs = self.source.pairs_nodirectives
        self.origins = origin_lines(pp)
        self.sites = function_definition_sites(self.source)
        self.bodies = sorted(
            (_Body(site.body_open, self.pairs[site.body_open]) for site in self.sites if site.body_open in self.pairs),
            key=lambda body: body.open,
        )
        self._opens = [body.open for body in self.bodies]

    def origin(self, offset: int) -> Origin | None:
        index = self.source.line_of(offset) - 1
        return self.origins[index] if 0 <= index < len(self.origins) else None

    def where(self, offset: int) -> str:
        origin = self.origin(offset)
        return f"{origin.file}:{origin.line}" if origin else "?"

    def body_of(self, offset: int) -> _Body | None:
        index = bisect.bisect_right(self._opens, offset) - 1
        if index >= 0 and self.bodies[index].open < offset < self.bodies[index].close:
            return self.bodies[index]
        return None

    def in_body(self, offset: int) -> bool:
        return self.body_of(offset) is not None

    def context_of(self, offset: int) -> Scope:
        if self.in_body(offset):
            return Scope.BODY
        enclosing = [
            opening
            for opening, closing in self.pairs.items()
            if self.code[opening] == BLOCK_OPEN and opening < offset < closing
        ]
        if not enclosing:
            return Scope.FILE
        if _RECORD_HEAD_RE.search(self.code, 0, max(enclosing)) is not None:
            return Scope.RECORD
        return Scope.UNKNOWN_BLOCK

    def is_member(self, offset: int) -> bool:
        position = offset
        while position > 0 and self.code[position - 1].isspace():
            position -= 1
        before = self.code[max(0, position - len(MEMBER_ARROW)) : position]
        return before.endswith(MEMBER_DOT) or before.endswith(MEMBER_ARROW)

    def call_open(self, end: int) -> int | None:
        token = _NON_SPACE_RE.search(self.code, end)
        if token is None or self.code[token.start()] != CALL_OPEN:
            return None
        return token.start()

    def call_close(self, end: int) -> int | None:
        opening = self.call_open(end)
        return None if opening is None else self.pairs.get(opening)

    def has_top_level_comma(self, opening: int, closing: int) -> bool:
        position = opening + 1
        while position < closing:
            if self.code[position] == ARGUMENT_SEPARATOR:
                return True
            position = self.pairs.get(position, position) + 1
        return False

    def references(self, names: Sequence[str]) -> list[_Reference]:
        if not names:
            return []
        pattern = re.compile(r"(?<![\w$])(" + "|".join(re.escape(name) for name in names) + r")(?![\w$])")
        return [
            _Reference(match.group(1), match.start(), match.end())
            for match in pattern.finditer(self.code)
            if not self.is_member(match.start())
        ]


def _unclassified(view: _View, reference: _Reference) -> str:
    return (
        f"{view.where(reference.start)}: {reference.name} appears inside a block that is neither a recognised function "
        "body nor a struct or union member list (K&R definition, function returning a function pointer, initializer)"
    )


def body_references(pp: str, name: str) -> list[str]:
    view = _View(pp)
    return [
        view.where(reference.start)
        for reference in view.references([name])
        if view.context_of(reference.start) in (Scope.BODY, Scope.UNKNOWN_BLOCK)
    ]


def _main_problems(view: _View, harness: str) -> tuple[int | None, list[str]]:
    mains = [site for site in view.sites if site.name == ENTRY_POINT]
    if len(mains) != 1:
        return None, [f"the harness translation unit defines {ENTRY_POINT}() {len(mains)} times; it must define it once"]
    origin = view.origin(mains[0].offset)
    if origin is None or origin.file != harness:
        return None, [f"{ENTRY_POINT}() is defined at {view.where(mains[0].offset)}, not in the harness {harness}"]
    return mains[0].body_open, []


def _entry_point_problems(view: _View) -> list[str]:
    problems = []
    for reference in view.references([ENTRY_POINT]):
        scope = view.context_of(reference.start)
        if scope is Scope.BODY:
            problems.append(
                f"{view.where(reference.start)}: {ENTRY_POINT} is referenced inside a function body; calling "
                f"{ENTRY_POINT}() again would restart the claimed-call count"
            )
        elif scope is Scope.UNKNOWN_BLOCK:
            problems.append(_unclassified(view, reference))
    return problems


def _call_sites(view: _View, claimed: Sequence[str]) -> tuple[list[CallSite], list[str]]:
    calls: list[CallSite] = []
    problems: list[str] = []
    for reference in view.references(claimed):
        scope = view.context_of(reference.start)
        close = view.call_close(reference.end)
        if scope is Scope.BODY and close is not None:
            calls.append(CallSite(reference.name, reference.start, close, view.origin(reference.start)))
        elif scope is Scope.UNKNOWN_BLOCK:
            problems.append(_unclassified(view, reference))
        elif close is None and scope is not Scope.RECORD:
            problems.append(
                f"{view.where(reference.start)}: claimed function {reference.name} is used other than in a direct call "
                "(address taken, function pointer or shadowing name); claimed calls must be direct calls"
            )
    return calls, problems


def _assume_sites(view: _View) -> tuple[list[AssumeSite], list[str]]:
    assumes: list[AssumeSite] = []
    problems: list[str] = []
    for reference in view.references([ASSUME_BUILTIN]):
        scope = view.context_of(reference.start)
        if scope is Scope.UNKNOWN_BLOCK:
            problems.append(_unclassified(view, reference))
            continue
        if scope is not Scope.BODY:
            continue
        opening = view.call_open(reference.end)
        close = None if opening is None else view.pairs.get(opening)
        if opening is None or close is None:
            problems.append(f"{view.where(reference.start)}: {ASSUME_BUILTIN} is used other than in a direct call")
            continue
        if view.has_top_level_comma(opening, close):
            problems.append(f"{view.where(reference.start)}: {ASSUME_BUILTIN} must have exactly one argument")
            continue
        assumes.append(AssumeSite(reference.start, opening, close, view.origin(reference.start)))
    return assumes, problems


def _calls_inside_assumes(view: _View, calls: list[CallSite], assumes: list[AssumeSite]) -> list[str]:
    return [
        f"{view.where(call.start)}: claimed function {call.function} is called inside LEANC_ASSUME at "
        f"{view.where(assume.start)}; an assumption must not depend on a claimed call"
        for assume in assumes
        for call in calls
        if assume.open < call.start < assume.close
    ]


def _assume_order_problems(view: _View, calls: list[CallSite], assumes: list[AssumeSite]) -> list[str]:
    first_call: dict[_Body | None, CallSite] = {}
    for call in sorted(calls, key=lambda site: site.start):
        first_call.setdefault(view.body_of(call.start), call)
    problems = []
    for assume in assumes:
        first = first_call.get(view.body_of(assume.start))
        if first is not None and assume.start > first.start:
            problems.append(
                f"{view.where(assume.start)}: LEANC_ASSUME after the call of the claimed function {first.function} "
                f"at {first.where} in the same function; every harness assumption must come before the first "
                "claimed call"
            )
    return problems


def _reserved_identifier_problems(view: _View) -> list[str]:
    return [
        f"{view.where(match.start())}: {match.group()} uses the leanc_ prefix reserved for the inserted checks"
        for match in _RESERVED_IDENTIFIER_RE.finditer(view.code)
    ]


def _uncalled_problems(calls: list[CallSite], claimed: Sequence[str]) -> list[str]:
    called = {call.function for call in calls}
    return [f"claimed function {name} is never called from the harness" for name in claimed if name not in called]


def _first_line_problems(pp: str) -> list[str]:
    if linemarker(pp.partition("\n")[0]) is not None:
        return []
    return ["the preprocessed harness does not start with a linemarker, so injected lines would shift its line numbers"]


def harness_layout(pp: str, claimed: Sequence[str], harness: str) -> HarnessLayout:
    view = _View(pp)
    main_body_open, problems = _main_problems(view, harness)
    problems = _first_line_problems(pp) + problems
    problems += _entry_point_problems(view) + _reserved_identifier_problems(view)
    calls, call_problems = _call_sites(view, claimed)
    assumes, assume_problems = _assume_sites(view)
    problems += call_problems + assume_problems
    problems += _calls_inside_assumes(view, calls, assumes) + _assume_order_problems(view, calls, assumes)
    problems += _uncalled_problems(calls, claimed)
    return HarnessLayout(tuple(calls), tuple(assumes), main_body_open, tuple(problems))


def claimed_call_label(function: str) -> str:
    return f"{CLAIMED_CALL_LABEL_PREFIX}{function}"


def return_probe_label(function: str) -> str:
    return f"{RETURN_PROBE_PREFIX}{function}"


def _assertion(condition: str, label: str) -> str:
    return f'__CPROVER_assert({condition}, "{label}")'


def _claimed_call_prefix(function: str) -> str:
    check = _assertion(f"{CLAIMED_CALLS_COUNTER} == 0", claimed_call_label(function))
    return f"({check}, {CLAIMED_CALLS_COUNTER} = 1, "


def _call_wrap(call: CallSite, returns: Mapping[str, ReturnKind] | None) -> _Wrap:
    prefix = _claimed_call_prefix(call.function)
    suffix = ")"
    if returns is not None:
        probe = _assertion("0", return_probe_label(call.function))
        if returns[call.function] is ReturnKind.VOID:
            suffix = f", {probe})"
        else:
            prefix += f"({{ __auto_type {RETURNED_VALUE} = "
            suffix = f"; {probe}; {RETURNED_VALUE}; }}))"
    return _Wrap(call.start, prefix, call.close + 1, suffix)


def _assume_wrap(assume: AssumeSite) -> _Wrap:
    check = _assertion(f"{CLAIMED_CALLS_COUNTER} == 0", ASSUME_AFTER_CLAIMED_CALL_LABEL)
    return _Wrap(assume.open + 1, f"({{ _Bool {ASSUMED_VALUE} = (", assume.close, f"); {check}; {ASSUMED_VALUE}; }})")


def _reset_wrap(main_body_open: int, end: int) -> _Wrap:
    return _Wrap(main_body_open + 1, f" {CLAIMED_CALLS_COUNTER} = 0; ", end, "")


def _insertions(pp: str, layout: HarnessLayout, returns: Mapping[str, ReturnKind] | None) -> dict[int, list[str]]:
    if layout.main_body_open is None:
        raise ValueError("the harness layout has no main() body")
    wraps = [
        _reset_wrap(layout.main_body_open, len(pp) + 1),
        *(_call_wrap(call, returns) for call in layout.calls),
        *(_assume_wrap(assume) for assume in layout.assumes),
    ]
    keyed: dict[int, list[tuple[tuple[int, int], str]]] = {}
    for wrap in wraps:
        keyed.setdefault(wrap.prefix_at, []).append(((1, -wrap.span), wrap.prefix))
        if wrap.suffix:
            keyed.setdefault(wrap.suffix_at, []).append(((0, wrap.span), wrap.suffix))
    return {offset: [text for _, text in sorted(pieces, key=lambda piece: piece[0])] for offset, pieces in keyed.items()}


def _linemarker(harness: str) -> str:
    escaped = "".join(LINEMARKER_ESCAPES.get(char, char) for char in harness)
    return f'# 1 "{escaped}"'


def instrumented_harness(
    pp: str, layout: HarnessLayout, harness: str, returns: Mapping[str, ReturnKind] | None = None
) -> str:
    if layout.problems:
        raise ValueError("the harness layout has problems")
    pieces = _insertions(pp, layout, returns)
    out: list[str] = []
    end = len(pp)
    for offset in sorted(pieces, reverse=True):
        out += [pp[offset:end], "".join(pieces[offset])]
        end = offset
    out.append(pp[:end])
    body = "".join(reversed(out))
    return f"{_linemarker(harness)}\nstatic int {CLAIMED_CALLS_COUNTER};\n{body}"
