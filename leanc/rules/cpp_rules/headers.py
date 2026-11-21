"""What the protected C++ headers declare: identifiers, macros and operators."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from leanc.lang.cpp_lexer import CppSource
from leanc.rules.preprocessor import defined_macro_names, identifiers

__all__ = ["HeaderFacts", "header_facts"]

_OPERATOR_RE = re.compile(
    r"\boperator\b\s*(?:(?P<kw>new|delete|co_await)\b\s*(?P<arr>\[\s*\])?"
    r"|(?P<sym>\(\s*\)|\[\s*\]|->\*?|<=>|<<=?|>>=?|&&|\|\||\+\+|--|[-+*/%^&|!=<>]=?|,|~)"
    r'|(?P<lit>""\s*\w+)'
    r"|(?P<conv>[A-Za-z_]\w*))"
)


def _demangler_spelling(operator: re.Match[str]) -> str:
    if operator.group("kw"):
        return "operator " + operator.group("kw") + ("[]" if operator.group("arr") else "")
    if operator.group("sym"):
        return "operator" + re.sub(r"\s+", "", operator.group("sym"))
    if operator.group("lit"):
        return 'operator""' + operator.group("lit")[2:].strip()
    return "operator " + operator.group("conv")


@dataclass
class HeaderFacts:
    """What the protected headers declare (comment- and literal-free views)."""

    idents: set[str] = field(default_factory=set)
    macros: set[str] = field(default_factory=set)
    operators: set[str] = field(default_factory=set)


def header_facts(headers: dict[str, str] | None) -> HeaderFacts:
    facts = HeaderFacts()
    for text in (headers or {}).values():
        src = CppSource(text)
        facts.idents |= identifiers(src.code)
        facts.macros |= defined_macro_names(src.code)
        facts.operators |= {_demangler_spelling(op) for op in _OPERATOR_RE.finditer(src.code_nodirectives)}
    return facts
