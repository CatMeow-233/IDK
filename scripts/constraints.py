from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import asdict
from pathlib import Path

from scripts.ruleset.config import load_constraints
from scripts.ruleset.core import ConstraintsError, Violation
from scripts.ruleset.fix import fix
from scripts.ruleset.registry import check

REPO = Path(__file__).resolve().parent.parent
DESCRIPTION = "check (and --fix) the repository constraints declared in constraints.toml"
EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_INVALID_CONSTRAINTS = 2


def _report(violations: list[Violation], fixed: list[str], unfixable: list[str], as_json: bool) -> None:
    if as_json:
        report = {"ok": not violations, "violations": [asdict(v) for v in violations], "fixed": fixed, "unfixable": unfixable}
        print(json.dumps(report, indent=2))
        return
    for path in fixed:
        print(f"fixed {path}")
    for path in unfixable:
        print(f"unfixable {path} (code would change; edit by hand)")
    for violation in violations:
        print(violation.render())
    print(f"{len(violations)} violation(s)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.constraints", description=DESCRIPTION)
    parser.add_argument("--fix", action="store_true", help="remove comments/docstrings and rewrite the CLAUDE.md layer block")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--root", type=Path, default=REPO, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        c = load_constraints(args.root)
    except (ConstraintsError, tomllib.TOMLDecodeError, OSError, SyntaxError) as exc:
        print(f"constraints: {exc}", file=sys.stderr)
        return EXIT_INVALID_CONSTRAINTS
    fixed, unfixable = fix(c) if args.fix else ([], [])
    violations = check(c)
    _report(violations, fixed, unfixable, args.json)
    return EXIT_VIOLATIONS if violations else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
