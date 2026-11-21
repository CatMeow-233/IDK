from __future__ import annotations

from leanc.foundation.models import PolicyViolation
from leanc.rules.c_rules import c_source_violations
from leanc.rules.cpp_rules.source_rules import cpp_source_violations
from leanc.specs.model import Spec

__all__ = ["source_violations"]


def source_violations(
    spec: Spec, rel: str, text: str, *, headers: dict[str, str] | None = None
) -> list[PolicyViolation]:
    if spec.language.is_cpp:
        return cpp_source_violations(spec, rel, text, headers=headers)
    return c_source_violations(spec, rel, text, headers=headers)
