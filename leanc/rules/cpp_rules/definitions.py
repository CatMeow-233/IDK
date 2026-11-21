"""Namespace-scope C++ function definitions and the kind of brace scope every offset lies in."""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, NamedTuple

from leanc.lang.c_lexer import CSource
from leanc.lang.cpp_lexer import CppSource
from leanc.rules.definitions import NOT_A_FUNCTION, FunctionDefinition, is_trivial_body

__all__ = [
    "CppDefinition",
    "Scope",
    "Scopes",
    "cpp_function_definitions",
    "declaration_ends",
    "find_cpp_function_definitions",
    "identifier_start_before",
    "space_start_before",
]

_MAX_HEAD_SCAN_CHARS = 512
_MAX_DECLARATOR_SCAN_CHARS = 4096
_MAX_QUALIFIER_PARTS = 64
_MAX_DECLARATOR_STEPS = 256
_MAX_MEMBER_INITIALIZERS = 256
_ATTRIBUTE_LOOKAHEAD_CHARS = 40
_WORD_LOOKAHEAD_CHARS = 64
_INITIALIZER_LOOKAHEAD_CHARS = 400
_DEFAULTED_LOOKAHEAD_CHARS = 40


class Scope(StrEnum):
    GLOBAL = "global"
    NAMESPACE = "namespace"
    ANONYMOUS = "anonymous"
    EXTERN_C = "extern_c"
    CLASS = "class"
    OTHER = "other"


_NON_NAMESPACE_SCOPES = (Scope.CLASS, Scope.OTHER)

_NAMESPACE_HEAD_RE = re.compile(r"(?:\binline\s+)?\bnamespace\b(?:\s+[\w:]+)?\s*(?:\[\[[^\]]*\]\]\s*)?$")
_ANONYMOUS_NAMESPACE_HEAD_RE = re.compile(r"namespace\s*$")
_LINKAGE_HEAD_RE = re.compile(r'\bextern\s*"(C|C\+\+)"\s*$')
_CLASS_BODY_HEAD_RE = re.compile(r"\b(?:class|struct|union)\b[^()]*$")
_DECL_END_RE = re.compile(r"[{;]")
_FN_NAME_RE = re.compile(
    r"(?<![\w.])(?<!->)(?:(~)\s*)?\b(operator\b\s*(?:new\b|delete\b)\s*(?:\[\s*\])?|[A-Za-z_]\w*)\s*\("
)
_ATTRIBUTE_RE = re.compile(r"(?:__attribute__|__attribute|__declspec)\s*\(")
_WORD_RE = re.compile(r"[A-Za-z_]\w*")
_MEMBER_INITIALIZER_NAME_RE = re.compile(r"(?:::)?[A-Za-z_][\w:]*\s*(?:<[^;{}()]*>)?\s*")
_NOT_A_FUNCTION = NOT_A_FUNCTION | frozenset(
    {"decltype", "noexcept", "requires", "typeid", "catch", "throw", "new", "delete", "return", "co_return",
     "co_await", "co_yield", "static_cast", "dynamic_cast", "const_cast", "reinterpret_cast", "template",
     "explicit", "operator", "case", "do", "else", "using", "alignas", "__attribute__", "assert"}
)
_DECLARATOR_TAIL_WORDS = frozenset({"const", "volatile", "override", "final", "mutable", "constexpr", "consteval"})
_EXCEPTION_SPECIFIERS = ("noexcept", "throw")
_REQUIRES_CLAUSE = "requires"
_DEFAULTED_OR_DELETED = ("default", "delete")
_DESTRUCTOR_MARK = "~"


def declaration_ends(code: str) -> list[int]:
    return [end.start() for end in _DECL_END_RE.finditer(code)]


def _blank_directive_lines(view: str, code: str) -> str:
    return "\n".join(
        re.sub(r"[^\n]", " ", v) if c.lstrip().startswith("#") else v
        for v, c in zip(view.split("\n"), code.split("\n"))
    )


def _scope_kind(head: str, code_head: str) -> Scope:
    linkage = _LINKAGE_HEAD_RE.search(head)
    if linkage:
        return Scope.EXTERN_C if linkage.group(1) == "C" else Scope.GLOBAL
    if _NAMESPACE_HEAD_RE.search(code_head):
        return Scope.ANONYMOUS if _ANONYMOUS_NAMESPACE_HEAD_RE.search(code_head) else Scope.NAMESPACE
    if _CLASS_BODY_HEAD_RE.search(code_head):
        return Scope.CLASS
    return Scope.OTHER


class _ScopeState(NamedTuple):
    open_non_namespace_scopes: int
    innermost: Scope


class Scopes:
    """The kind of every ``{`` of a file: namespace-like (``namespace``, linkage blocks) or class / function / other."""

    def __init__(self, src: CSource):
        code = src.code_nodirectives
        noc = _blank_directive_lines(src.nocomment, src.code)
        self._marks: list[int] = []
        self._states: list[_ScopeState] = []
        stack: list[Scope] = []
        non_namespace = 0
        last_statement_end = 0
        for brace in re.finditer(r"[{};]", code):
            ch = brace.group()
            if ch == "{":
                head_start = max(last_statement_end, brace.start() - _MAX_HEAD_SCAN_CHARS)
                kind = _scope_kind(noc[head_start : brace.start()], code[head_start : brace.start()])
                stack.append(kind)
                non_namespace += kind in _NON_NAMESPACE_SCOPES
            elif ch == "}" and stack:
                non_namespace -= stack.pop() in _NON_NAMESPACE_SCOPES
            last_statement_end = brace.end()
            if ch != ";":
                self._marks.append(brace.start())
                self._states.append(_ScopeState(non_namespace, stack[-1] if stack else Scope.GLOBAL))

    def _state_before(self, offset: int) -> _ScopeState | None:
        j = bisect.bisect_left(self._marks, offset) - 1
        return self._states[j] if j >= 0 else None

    def innermost(self, offset: int) -> Scope:
        state = self._state_before(offset)
        return state.innermost if state else Scope.GLOBAL

    def at(self, offset: int) -> Scope | None:
        """The namespace-like scope at ``offset``, or None inside a class, function or initializer."""
        state = self._state_before(offset)
        if state is None:
            return Scope.GLOBAL
        return None if state.open_non_namespace_scopes else state.innermost


@dataclass(frozen=True)
class CppDefinition:
    """A namespace-scope function definition; ``body`` is "" for ``= default`` / ``= delete``."""

    name: str
    qualifier: str
    offset: int
    scope: Scope
    body: str
    has_body: bool


def space_start_before(code: str, k: int) -> int:
    while k > 0 and code[k - 1].isspace():
        k -= 1
    return k


def identifier_start_before(code: str, k: int) -> int:
    while k > 0 and (code[k - 1].isalnum() or code[k - 1] == "_"):
        k -= 1
    return k


def _template_arguments_start_before(code: str, k: int) -> int:
    depth = 0
    stop = max(0, k - _MAX_HEAD_SCAN_CHARS)
    while k > stop:
        k -= 1
        if code[k] == ">":
            depth += 1
        elif code[k] == "<":
            depth -= 1
            if depth == 0:
                break
    return k


def _qualifier_before(code: str, start: int) -> str:
    """The ``A<..>::B::`` (or ``::``) written right before ``start``, without whitespace."""
    j = start
    parts: list[str] = []
    for _ in range(_MAX_QUALIFIER_PARTS):
        k = space_start_before(code, j)
        if code[max(0, k - 2) : k] != "::":
            break
        k -= 2
        end = k
        k = space_start_before(code, k)
        if k > 0 and code[k - 1] == ">":
            k = space_start_before(code, _template_arguments_start_before(code, k))
        name_end = k
        k = identifier_start_before(code, k)
        parts.append(re.sub(r"\s+", "", code[k:end]) + "::")
        j = k
        if name_end == k:
            break
    return "".join(reversed(parts))


class _BodySpan(NamedTuple):
    open: int
    close: int


_Defaulted = Literal["defaulted"]


class _DeclaratorTail:
    """Scans what follows a function's parameter list up to its body."""

    def __init__(self, code: str, pairs: dict[int, int], ends: list[int]):
        self._code = code
        self._pairs = pairs
        self._ends = ends

    def _skip_space(self, j: int) -> int:
        while j < len(self._code) and self._code[j].isspace():
            j += 1
        return j

    def _declaration_end(self, k: int) -> int | None:
        e = bisect.bisect_left(self._ends, k)
        if e < len(self._ends) and self._ends[e] - k <= _MAX_DECLARATOR_SCAN_CHARS:
            return self._ends[e]
        return None

    def _body_at(self, j: int) -> _BodySpan:
        close = self._pairs.get(j)
        return _BodySpan(j, close if close is not None else len(self._code))

    def _after_attribute(self, j: int) -> int | None:
        code = self._code
        if code.startswith("[[", j) and (close := self._pairs.get(j)) is not None:
            return close + 1
        attribute = _ATTRIBUTE_RE.match(code[j : j + _ATTRIBUTE_LOOKAHEAD_CHARS])
        if attribute and (close := self._pairs.get(j + attribute.end() - 1)) is not None:
            return close + 1
        return None

    def _after_exception_specifier(self, j: int) -> int:
        k = self._skip_space(j)
        if k < len(self._code) and self._code[k] == "(" and (close := self._pairs.get(k)) is not None:
            return close + 1
        return j

    def _after_member_initializers(self, j: int) -> int | None:
        code, n = self._code, len(self._code)
        for _ in range(_MAX_MEMBER_INITIALIZERS):
            j = self._skip_space(j)
            member = _MEMBER_INITIALIZER_NAME_RE.match(code[j : j + _INITIALIZER_LOOKAHEAD_CHARS])
            if not member:
                return None
            j += member.end()
            if not (j < n and code[j] in "({" and (close := self._pairs.get(j)) is not None):
                return None
            j = self._skip_space(close + 1)
            if code.startswith("...", j):
                j = self._skip_space(j + 3)
            if j < n and code[j] == ",":
                j += 1
                continue
            break
        return j

    def _after_word(self, word: str, j: int) -> int | None:
        if word in _DECLARATOR_TAIL_WORDS:
            return j
        if word in _EXCEPTION_SPECIFIERS:
            return self._after_exception_specifier(j)
        if word == _REQUIRES_CLAUSE:
            return self._declaration_end(j)
        return None

    def body_after(self, j: int) -> _BodySpan | _Defaulted | None:
        """The body after the parameter list closing at ``j - 1``; "defaulted" for ``= default`` / ``= delete``."""
        code, n = self._code, len(self._code)
        for _ in range(_MAX_DECLARATOR_STEPS):
            j = self._skip_space(j)
            if j >= n:
                return None
            c = code[j]
            if c == "{":
                return self._body_at(j)
            after_attribute = self._after_attribute(j)
            if after_attribute is not None:
                j = after_attribute
                continue
            word = _WORD_RE.match(code[j : j + _WORD_LOOKAHEAD_CHARS])
            if word:
                after_word = self._after_word(word.group(), j + len(word.group()))
                if after_word is None:
                    return None
                j = after_word
                continue
            if code.startswith("&&", j):
                j += 2
                continue
            if c == "&":
                j += 1
                continue
            if code.startswith("->", j):
                trailing_return_end = self._declaration_end(j + 2)
                if trailing_return_end is None:
                    return None
                j = trailing_return_end
                continue
            if c == ":" and not code.startswith("::", j):
                return self._body_after_member_initializers(j + 1)
            if c == "=":
                rest = code[j + 1 : j + _DEFAULTED_LOOKAHEAD_CHARS].lstrip()
                return "defaulted" if rest.startswith(_DEFAULTED_OR_DELETED) else None
            return None
        return None

    def _body_after_member_initializers(self, j: int) -> _BodySpan | None:
        after = self._after_member_initializers(j)
        if after is not None and after < len(self._code) and self._code[after] == "{":
            return self._body_at(after)
        return None


def _normalized_function_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", raw).replace(" [", "[").replace("[ ", "[").replace(" ]", "]")
    if name.startswith("operator"):
        name = re.sub(r"^operator\s*", "operator ", name)
    return name


def cpp_function_definitions(src: CSource) -> list[CppDefinition]:
    """Function definitions at namespace scope, including out-of-line members (not those in class bodies or macros)."""
    code = src.code_nodirectives
    pairs = src.pairs_nodirectives
    scopes = Scopes(src)
    tail = _DeclaratorTail(code, pairs, declaration_ends(code))
    out: list[CppDefinition] = []
    for m in _FN_NAME_RE.finditer(code):
        if m.group(2) in _NOT_A_FUNCTION:
            continue
        scope = scopes.at(m.start())
        if scope is None:
            continue
        close = pairs.get(m.end() - 1)
        if close is None:
            continue
        body = tail.body_after(close + 1)
        if body is None:
            continue
        destructor = bool(m.group(1))
        name = (_DESTRUCTOR_MARK if destructor else "") + _normalized_function_name(m.group(2))
        start = m.start(1) if destructor else m.start(2)
        qualifier = _qualifier_before(code, start)
        if body == "defaulted":
            out.append(CppDefinition(name, qualifier, start, scope, "", False))
        else:
            out.append(CppDefinition(name, qualifier, start, scope, code[body.open + 1 : body.close], True))
    return out


def find_cpp_function_definitions(src: CSource, name: str, file: str = "") -> list[FunctionDefinition]:
    """C++ counterpart of :func:`leanc.rules.definitions.find_function_definitions` (unqualified ``name``)."""
    if not isinstance(src, CppSource):
        src = CppSource(src.raw)
    return [
        FunctionDefinition(file, src.line_of(d.offset), d.body, is_trivial_body(d.body))
        for d in cpp_function_definitions(src)
        if d.name == name and d.has_body
    ]
