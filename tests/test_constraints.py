from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from scripts.constraints import (
    CONSTRAINTS_FILE,
    RULES,
    ConstraintsError,
    check,
    expected_layer_block,
    fix,
    fixed_source,
    load_constraints,
    main,
)

REPO = Path(__file__).resolve().parent.parent
REAL_CONSTRAINTS = (REPO / CONSTRAINTS_FILE).read_text(encoding="utf-8")
BASELINE = {
    "leanc/__init__.py": "",
    "leanc/foundation/__init__.py": "",
    "leanc/foundation/a.py": "VALUE = 1\n",
    "leanc/specs/__init__.py": "",
    "leanc/specs/b.py": "from leanc.foundation.a import VALUE\n\nDOUBLE = VALUE * 2\n",
    "leanc/cli.py": (
        "def main() -> int:\n"
        "    try:\n"
        "        return 0\n"
        "    except Exception:\n"
        "        return 70\n"
    ),
    "tests/test_x.py": "def test_x():\n    assert True\n",
    "scripts/tool.py": "#!/usr/bin/env python3\nVALUE = 1\n",
}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def make_repo(root: Path, files: dict[str, str] | None = None, constraints: str = REAL_CONSTRAINTS):
    _write(root, {CONSTRAINTS_FILE: constraints, **BASELINE, **(files or {})})
    c = load_constraints(root)
    if not (root / c.directives_file).exists():
        (root / c.directives_file).write_text("\n".join(["# d", *expected_layer_block(c), ""]), encoding="utf-8")
    return c


def rules_hit(c) -> set[str]:
    return {v.rule for v in check(c)}


def test_baseline_repository_is_clean(tmp_path):
    assert check(make_repo(tmp_path)) == []


MUTATIONS = {
    "layer-known": {"leanc/mystery/x.py": "VALUE = 1\n"},
    "layer-dependency": {"leanc/foundation/up.py": "from leanc.specs.b import DOUBLE\n"},
    "import-top-level": {"leanc/specs/lazy.py": "def f():\n    import leanc.foundation.a\n    return leanc\n"},
    "import-cycle": {
        "leanc/foundation/c1.py": "from leanc.foundation import c2\n",
        "leanc/foundation/c2.py": "from leanc.foundation import c1\n",
    },
    "no-comments": {"tests/test_y.py": "VALUE = 1  # note\n"},
    "no-docstrings": {"scripts/doc.py": '"""module doc"""\nVALUE = 1\n'},
    "max-file-lines": {"leanc/foundation/big.py": "VALUE = 1\n" * 601},
    "no-catch-all": {"leanc/foundation/h.py": "def f():\n    try:\n        return 1\n    except Exception as exc:\n        raise ValueError() from exc\n"},
    "no-swallow": {"leanc/foundation/s.py": "def f():\n    try:\n        return 1\n    except ValueError:\n        pass\n"},
}


def test_every_rule_has_a_mutation_test():
    assert set(MUTATIONS) | {"directives-sync"} == set(RULES)


@pytest.mark.parametrize("rule", sorted(MUTATIONS))
def test_single_violation_is_reported_by_exactly_its_rule(tmp_path, rule):
    assert rules_hit(make_repo(tmp_path, MUTATIONS[rule])) == {rule}


def test_directives_drift_is_reported_and_fixed(tmp_path):
    c = make_repo(tmp_path)
    claude = tmp_path / c.directives_file
    claude.write_text(claude.read_text().replace("- specs: foundation", "- specs: foundation lang"))
    assert rules_hit(c) == {"directives-sync"}
    fixed, unfixable = fix(c)
    assert fixed == [c.directives_file] and unfixable == [] and check(c) == []


def test_missing_layer_block_is_reported(tmp_path):
    c = make_repo(tmp_path, {"CLAUDE.md": "# d\n"})
    assert rules_hit(c) == {"directives-sync"}


@pytest.mark.parametrize(
    "files",
    [
        {"tests/fixtures/attack.py": "VALUE = 1  # fixture comment\n"},
        {"leanc/foundation/ok.py": "def f():\n    try:\n        return 1\n    except FileNotFoundError:\n        pass\n"},
        {"leanc/foundation/text.py": 'TEXT = "# not a comment"\n'},
    ],
)
def test_allowed_patterns_are_not_violations(tmp_path, files):
    assert check(make_repo(tmp_path, files)) == []


def test_bare_except_is_rejected_even_in_the_entry_point(tmp_path):
    cli = "def main() -> int:\n    try:\n        return 0\n    except:\n        return 70\n"
    assert rules_hit(make_repo(tmp_path, {"leanc/cli.py": cli})) == {"no-catch-all"}


def test_catch_all_outside_the_entry_point_function_is_rejected(tmp_path):
    cli = BASELINE["leanc/cli.py"] + "\n\ndef helper():\n    try:\n        return 0\n    except BaseException:\n        raise\n"
    assert rules_hit(make_repo(tmp_path, {"leanc/cli.py": cli})) == {"no-catch-all"}


@pytest.mark.parametrize(
    "edit, message",
    [
        (lambda t: t.replace("max_file_lines = 600", "max_file_lines = 600\nmax_file_line = 1"), "keys must be exactly"),
        (lambda t: t.replace("max_file_lines = 600", "max_file_lines = true"), "must be int"),
        (lambda t: t + "\n[extra]\nx = 1\n", "unknown tables"),
        (lambda t: t.replace('specs = ["foundation"]', 'specs = ["foundation", "ghost"]'), "undeclared layers"),
        (lambda t: t.replace('foundation = []', 'foundation = ["specs"]'), "dependency cycle"),
        (lambda t: t.replace('specs = ["foundation"]', 'specs = ["specs"]'), "depends on itself"),
        (lambda t: t.replace('"leanc/cli.py:main"', '"leanc/cli.py"'), "path.py:function"),
        (lambda t: t.replace("[source]\nmax_file_lines = 600\n", ""), "missing table"),
    ],
)
def test_invalid_constraints_file_is_rejected(tmp_path, edit, message):
    with pytest.raises(ConstraintsError, match=message):
        make_repo(tmp_path, constraints=edit(REAL_CONSTRAINTS))


SOURCE_WITH_COMMENTS_AND_DOCSTRINGS = '''#!/usr/bin/env python3
"""module doc"""
import os  # trailing


# full line
class A:
    """class doc"""

    def f(self, x):
        """function
        doc"""
        text = "# kept"
        return x  # gone


def g():
    return os.sep
'''


def test_fixed_source_removes_comments_and_docstrings_keeping_the_code():
    fixed = fixed_source(SOURCE_WITH_COMMENTS_AND_DOCSTRINGS)
    assert fixed is not None
    assert "trailing" not in fixed and "full line" not in fixed and "doc" not in fixed
    assert '"# kept"' in fixed and fixed.startswith("#!/usr/bin/env python3\n")
    original = ast.parse(SOURCE_WITH_COMMENTS_AND_DOCSTRINGS)
    for node in ast.walk(original):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and ast.get_docstring(node) is not None:
            node.body.pop(0)
    assert ast.dump(original) == ast.dump(ast.parse(fixed))


@pytest.mark.parametrize(
    "source",
    [
        'def f():\n    """only a docstring"""\n',
        'def f(): "doc"\n    \n',
        'def f():\n    """doc"""; return 1\n',
    ],
)
def test_fixed_source_refuses_when_code_would_change(source):
    assert fixed_source(source) is None


def test_fix_rewrites_fixable_files_and_reports_unfixable_ones(tmp_path):
    fixable = "VALUE = 1  # note\n"
    unfixable = 'def f():\n    """only"""\n'
    c = make_repo(tmp_path, {"tests/test_a.py": fixable, "tests/test_b.py": unfixable})
    fixed, refused = fix(c)
    assert fixed == ["tests/test_a.py"] and refused == ["tests/test_b.py"]
    assert (tmp_path / "tests/test_a.py").read_text() == "VALUE = 1\n"
    assert (tmp_path / "tests/test_b.py").read_text() == unfixable
    assert rules_hit(c) == {"no-docstrings"}


def test_cli_exit_codes_and_json(tmp_path, capsys):
    make_repo(tmp_path)
    assert main(["--root", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    _write(tmp_path, MUTATIONS["no-comments"])
    assert main(["--root", str(tmp_path), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert [v["rule"] for v in report["violations"]] == ["no-comments"]
    (tmp_path / CONSTRAINTS_FILE).write_text("[package\n")
    assert main(["--root", str(tmp_path)]) == 2
