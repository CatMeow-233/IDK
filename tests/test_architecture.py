from __future__ import annotations

import pytest

from scripts.constraints import RULES, check, load_constraints

CONSTRAINTS = load_constraints()
MAX_SHOWN = 200


@pytest.mark.parametrize("rule", sorted(RULES))
def test_repository_satisfies_constraint(rule):
    violations = check(CONSTRAINTS, [rule])
    assert not violations, f"{len(violations)} violation(s):\n" + "\n".join(v.render() for v in violations[:MAX_SHOWN])
