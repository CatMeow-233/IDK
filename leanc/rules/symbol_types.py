from __future__ import annotations

__all__ = [
    "INDIRECT_FUNCTION_TYPES",
    "STRONG_TEXT_TYPE",
    "TEXT_DEFINITION_TYPES",
    "UNDEFINED_TYPES",
    "WEAK_DEFINITION_TYPES",
    "WEAK_UNDEFINED_TYPES",
    "defined_elsewhere",
    "has_external_linkage",
    "is_defined_global",
]

UNDEFINED_SYMBOL_TYPE = "U"
UNDEFINED_TYPES = frozenset({UNDEFINED_SYMBOL_TYPE, "w", "v"})
WEAK_UNDEFINED_TYPES = frozenset({"w", "v"})
WEAK_DEFINITION_TYPES = frozenset({"W", "V"})
INDIRECT_FUNCTION_TYPES = frozenset({"i", "I"})
STRONG_TEXT_TYPE = "T"
TEXT_DEFINITION_TYPES = frozenset({STRONG_TEXT_TYPE, "W"})


def has_external_linkage(nm_type: str) -> bool:
    return nm_type.isupper() or nm_type in INDIRECT_FUNCTION_TYPES


def is_defined_global(nm_type: str) -> bool:
    return nm_type.isupper() and nm_type != UNDEFINED_SYMBOL_TYPE


def defined_elsewhere(defined_per_file: dict[str, set[str]], rel: str) -> set[str]:
    if len(defined_per_file) <= 1:
        return set()
    return set().union(*(names for other, names in defined_per_file.items() if other != rel))
