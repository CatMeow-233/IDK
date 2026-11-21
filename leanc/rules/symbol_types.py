"""The ``nm -P`` symbol-type letters that the C and C++ object rules distinguish."""

from __future__ import annotations

__all__ = [
    "INDIRECT_FUNCTION_TYPES",
    "STRONG_TEXT_TYPE",
    "TEXT_DEFINITION_TYPES",
    "UNDEFINED_TYPES",
    "WEAK_DEFINITION_TYPES",
    "WEAK_UNDEFINED_TYPES",
    "has_external_linkage",
]

UNDEFINED_TYPES = frozenset({"U", "w", "v"})
WEAK_UNDEFINED_TYPES = frozenset({"w", "v"})
WEAK_DEFINITION_TYPES = frozenset({"W", "V"})
INDIRECT_FUNCTION_TYPES = frozenset({"i", "I"})
STRONG_TEXT_TYPE = "T"
TEXT_DEFINITION_TYPES = frozenset({STRONG_TEXT_TYPE, "W"})


def has_external_linkage(nm_type: str) -> bool:
    return nm_type.isupper() or nm_type in INDIRECT_FUNCTION_TYPES
