"""Absolute source-level integrity rules of one editable file, for the spec's language (C or C++)."""

from __future__ import annotations

from leanc.foundation.models import PolicyViolation
from leanc.rules.c_rules import c_source_violations
from leanc.rules.cpp_rules import cpp_source_violations
from leanc.specs.model import Spec

__all__ = ["source_violations"]


def source_violations(
    spec: Spec, rel: str, text: str, *, headers: dict[str, str] | None = None
) -> list[PolicyViolation]:
    """Absolute source-level integrity rules for one editable file.

    Pure (no tools): directives, includes, ``_Pragma``, forbidden identifiers
    and attributes, forbidden definitions, divergent predefined macros.
    ``headers`` (protected header texts) enables the "redefines an interface
    macro" check.  Used by :func:`leanc.backends.integrity.run_integrity` and by the repair /
    generation policy."""
    if spec.language.is_cpp:  # C++ variant of these rules (leanc.rules.cpp_rules)
        return cpp_source_violations(spec, rel, text, headers=headers)
    return c_source_violations(spec, rel, text, headers=headers)
