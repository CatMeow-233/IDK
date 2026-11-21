from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["DemangledName", "parse_demangled"]

_SPECIAL_PREFIXES: tuple[str, ...] = (
    "vtable for ",
    "VTT for ",
    "typeinfo for ",
    "typeinfo name for ",
    "typeinfo fn for ",
    "guard variable for ",
    "non-virtual thunk to ",
    "virtual thunk to ",
    "covariant return thunk to ",
    "TLS init function for ",
    "TLS wrapper function for ",
    "transaction clone for ",
    "template parameter object for ",
    "hidden alias for ",
)
_REFERENCE_TEMPORARY_RE = re.compile(r"^reference temporary #\d+ for ")
_CONSTRUCTION_VTABLE = "construction vtable for "
_CONSTRUCTION_VTABLE_SEPARATOR = "-in-"
_SYMBOLIC_OPERATORS = sorted(
    ["()", "[]", "->*", "->", "<=>", "<<=", ">>=", "<<", ">>", "&&", "||", "++", "--", "==", "!=", "<=", ">=",
     "+=", "-=", "*=", "/=", "%=", "^=", "&=", "|=", "+", "-", "*", "/", "%", "^", "&", "|", "~", "!", "=", "<",
     ">", ","],
    key=len,
    reverse=True,
)
_KEYWORD_OPERATORS = ("new[]", "delete[]", "new", "delete", "co_await")
_LITERAL_OPERATOR_QUOTES = '""'
_OPERATOR_WORD = "operator"
_ANONYMOUS_NAMESPACE = "(anonymous namespace)"
_SCOPE_SEPARATOR = "::"
_RETURN_TYPE_SEPARATOR = " "
_PARAMETER_LIST_OPEN = "("
_LOCAL_ENTITY_OPEN = "{"
_SKIPPED_GROUP_OPENERS = ("<", "[")
_DECLARATOR_CHARS = "*& "
_OPENERS = {"<": ">", "(": ")", "[": "]", "{": "}"}
_LOCAL_ENTITY_NAME_RE = re.compile(r"\{[^}#(]*")
_WORD_RE = re.compile(r"[A-Za-z_]\w*")


@dataclass(frozen=True)
class DemangledName:
    text: str
    components: tuple[str, ...]
    owner: tuple[str, ...]
    is_function: bool
    special: str | None = None
    inner: tuple[DemangledName, ...] = ()

    @property
    def simple(self) -> str:
        return self.components[-1] if self.components else ""


def _skip_group(s: str, i: int) -> int:
    stack = [_OPENERS[s[i]]]
    j = i + 1
    while j < len(s) and stack:
        c = s[j]
        if c in _OPENERS:
            stack.append(_OPENERS[c])
        elif c == stack[-1]:
            stack.pop()
        j += 1
    return j


def _skip_spaces(s: str, k: int) -> int:
    while k < len(s) and s[k] == " ":
        k += 1
    return k


def _is_identifier_char(c: str) -> bool:
    return c.isalnum() or c == "_"


def _is_operator_keyword_at(s: str, i: int) -> bool:
    end = i + len(_OPERATOR_WORD)
    return (
        s.startswith(_OPERATOR_WORD, i)
        and (i == 0 or not _is_identifier_char(s[i - 1]))
        and (end >= len(s) or not _is_identifier_char(s[end]))
    )


def _read_literal_operator(s: str, j: int) -> tuple[str, int]:
    k = _skip_spaces(s, j + len(_LITERAL_OPERATOR_QUOTES))
    e = k
    while e < len(s) and _is_identifier_char(s[e]):
        e += 1
    return 'operator""' + s[k:e], e


def _read_conversion_operator(s: str, k: int) -> tuple[str, int]:
    e = k
    while e < len(s) and s[e] != _PARAMETER_LIST_OPEN:
        e = _skip_group(s, e) if s[e] in "<[{" else e + 1
    converted_type = s[k:e].strip()
    word = _WORD_RE.match(converted_type)
    return "operator " + (word.group() if word else converted_type), e


def _read_operator(s: str, i: int) -> tuple[str, int]:
    j = i + len(_OPERATOR_WORD)
    rest = s[j:]
    for op in _SYMBOLIC_OPERATORS:
        if rest.startswith(op):
            return "operator" + op, j + len(op)
    if rest.startswith(_LITERAL_OPERATOR_QUOTES):
        return _read_literal_operator(s, j)
    k = _skip_spaces(s, j)
    for keyword in _KEYWORD_OPERATORS:
        if s.startswith(keyword, k):
            return "operator " + keyword, k + len(keyword)
    return _read_conversion_operator(s, k)


def _read_local_entity(s: str, i: int) -> tuple[str, int]:
    end = _skip_group(s, i)
    name = _LOCAL_ENTITY_NAME_RE.match(s[i:end])
    return (name.group() if name else "{") + "}", end


def _skip_declarator_prefix(s: str, i: int) -> int:
    i += 1
    while i < len(s) and s[i] in _DECLARATOR_CHARS:
        i += 1
    return i


def _skip_function_qualifiers(s: str, i: int) -> int:
    while i < len(s) and s[i] != ":":
        i += 1
    return i


def _parse_core(s: str) -> DemangledName | None:
    components: list[str] = []
    owner: tuple[str, ...] = ()
    current = ""
    is_function = False
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if _is_operator_keyword_at(s, i):
            current, i = _read_operator(s, i)
            continue
        if s.startswith(_ANONYMOUS_NAMESPACE, i):
            current = _ANONYMOUS_NAMESPACE
            i += len(_ANONYMOUS_NAMESPACE)
            continue
        if c == _LOCAL_ENTITY_OPEN:
            current, i = _read_local_entity(s, i)
            continue
        if c in _SKIPPED_GROUP_OPENERS:
            i = _skip_group(s, i)
            continue
        if c == _PARAMETER_LIST_OPEN and not current and not components:
            i = _skip_declarator_prefix(s, i)
            continue
        if c == _PARAMETER_LIST_OPEN:
            if current:
                components.append(current)
                current = ""
            if not owner:
                owner = tuple(components)
            is_function = True
            i = _skip_function_qualifiers(s, _skip_group(s, i))
            if s.startswith(_SCOPE_SEPARATOR, i):
                i += len(_SCOPE_SEPARATOR)
                is_function = False
                continue
            break
        if s.startswith(_SCOPE_SEPARATOR, i):
            if current:
                components.append(current)
            current = ""
            i += len(_SCOPE_SEPARATOR)
            continue
        if c == _RETURN_TYPE_SEPARATOR:
            components, current = [], ""
            i += 1
            continue
        current += c
        i += 1
    if current:
        components.append(current)
    if not components:
        return None
    return DemangledName(text=s, components=tuple(components), owner=owner or tuple(components),
                         is_function=is_function)


def _construction_vtable(s: str) -> DemangledName | None:
    parts = s[len(_CONSTRUCTION_VTABLE) :].split(_CONSTRUCTION_VTABLE_SEPARATOR)
    inner = tuple(p for p in (_parse_core(x) for x in parts) if p is not None)
    if len(inner) != len(parts):
        return None
    return DemangledName(text=s, components=inner[-1].components, owner=inner[-1].owner, is_function=False,
                         special=_CONSTRUCTION_VTABLE.strip(), inner=inner)


def _special_prefix(s: str) -> str | None:
    temporary = _REFERENCE_TEMPORARY_RE.match(s)
    if temporary:
        return temporary.group()
    return next((p for p in _SPECIAL_PREFIXES if s.startswith(p)), None)


def parse_demangled(text: str) -> DemangledName | None:
    s = text.strip()
    if s.startswith(_CONSTRUCTION_VTABLE):
        return _construction_vtable(s)
    prefix = _special_prefix(s)
    if prefix:
        inner_name = parse_demangled(s[len(prefix) :])
        if inner_name is None:
            return None
        return DemangledName(text=s, components=inner_name.components, owner=inner_name.owner,
                             is_function=False, special=prefix.strip(), inner=(inner_name,))
    return _parse_core(s)
