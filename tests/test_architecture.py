from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ruleset.config import load_constraints
from scripts.ruleset.registry import RULES, check

CONSTRAINTS = load_constraints(Path(__file__).resolve().parent.parent)
MAX_SHOWN = 200


@pytest.mark.parametrize("rule", sorted(RULES))
def test_repository_satisfies_constraint(rule):
    violations = check(CONSTRAINTS, [rule])
    assert not violations, f"{len(violations)} violation(s):\n" + "\n".join(v.render() for v in violations[:MAX_SHOWN])
