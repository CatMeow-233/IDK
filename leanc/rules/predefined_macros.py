from __future__ import annotations

import os
import re
from dataclasses import dataclass

from leanc.foundation.models import PolicyViolation
from leanc.lang.c_lexer import CSource
from leanc.rules.findings import Finding, to_violations

__all__ = [
    "GCC_MISSING",
    "PREDEFINED_MACRO_DUMP_ARGS",
    "MacroDefinition",
    "MacroDivergence",
    "PredefinedMacros",
    "divergent_macro_use_violations",
    "divergent_macros",
    "parse_predefined_macros",
]

GCC_MISSING = (
    "gcc not found: cannot compare the macros clang predefines for the compile gate with those gcc predefines "
    "when it preprocesses the code for CBMC or Frama-C"
)
PREDEFINED_MACRO_DUMP_ARGS: tuple[str, ...] = ("-dM", "-E", "-x", "c", os.devnull)
DIVERGENCE_RULE = "TOOL_DIVERGENCE"
UNDEFINED = "undefined"
EMPTY_DEFINITION = "defined empty"
MAX_SHOWN_DEFINITION_CHARS = 40
ELLIPSIS = "..."

_DEFINE_LINE_RE = re.compile(r"^#define (?P<name>[A-Za-z_]\w*)(?P<params>\([^)\n]*\))?(?: (?P<body>.*))?$")
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_]\w*\b")


@dataclass(frozen=True)
class MacroDefinition:
    parameters: str | None
    body: str

    def shown(self) -> str:
        text = f"{self.parameters or ''} {self.body}".strip()
        if not text:
            return EMPTY_DEFINITION
        if len(text) <= MAX_SHOWN_DEFINITION_CHARS:
            return text
        return text[:MAX_SHOWN_DEFINITION_CHARS] + ELLIPSIS


@dataclass(frozen=True)
class PredefinedMacros:
    definitions: dict[str, MacroDefinition]
    unparsed: tuple[str, ...]


@dataclass(frozen=True)
class MacroDivergence:
    name: str
    clang: MacroDefinition | None
    gcc: MacroDefinition | None
    verifier: str

    def message(self) -> str:
        return (
            f"'{self.name}' is predefined differently by clang with the compile gate's options ({_shown(self.clang)}) "
            f"and by gcc preprocessing for {self.verifier} ({_shown(self.gcc)}): the tools would verify different code"
        )


def _shown(definition: MacroDefinition | None) -> str:
    return UNDEFINED if definition is None else definition.shown()


def parse_predefined_macros(dump: str) -> PredefinedMacros:
    definitions: dict[str, MacroDefinition] = {}
    unparsed: list[str] = []
    for line in dump.splitlines():
        if not line.strip():
            continue
        match = _DEFINE_LINE_RE.match(line)
        if match is None:
            unparsed.append(line)
            continue
        body = " ".join((match.group("body") or "").split())
        definitions[match.group("name")] = MacroDefinition(match.group("params"), body)
    return PredefinedMacros(definitions, tuple(unparsed))


def divergent_macros(
    clang: dict[str, MacroDefinition], gcc: dict[str, MacroDefinition], verifier: str
) -> dict[str, MacroDivergence]:
    return {
        name: MacroDivergence(name, clang.get(name), gcc.get(name), verifier)
        for name in sorted(clang.keys() | gcc.keys())
        if clang.get(name) != gcc.get(name)
    }


def divergent_macro_use_violations(
    rel: str, src: CSource, divergent: dict[str, MacroDivergence]
) -> list[PolicyViolation]:
    found = [
        Finding(DIVERGENCE_RULE, divergent[use.group()].message(), use.start())
        for use in _IDENTIFIER_RE.finditer(src.code)
        if use.group() in divergent
    ]
    return to_violations(src, rel, found)
