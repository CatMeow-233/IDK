from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = "leanc"
TEST_DIR = REPO / "tests"
ALWAYS_RUN = ("tests/test_architecture.py",)
FULL_SUITE_TRIGGERS = ("tests/conftest.py", "pyproject.toml")
DESCRIPTION = "select tests affected by changed files via the leanc import graph"


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(REPO).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_modules(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return {name for name in found if name == PACKAGE_ROOT or name.startswith(PACKAGE_ROOT + ".")}


def _package_modules() -> dict[str, Path]:
    return {_module_name(p): p for p in sorted((REPO / PACKAGE_ROOT).rglob("*.py"))}


def _resolve(name: str, modules: dict[str, Path]) -> str | None:
    while name:
        if name in modules:
            return name
        name = name.rpartition(".")[0]
    return None


def _reverse_dependencies(modules: dict[str, Path]) -> dict[str, set[str]]:
    dependents: dict[str, set[str]] = defaultdict(set)
    for module, path in modules.items():
        for imported in _imported_modules(path):
            target = _resolve(imported, modules)
            if target and target != module:
                dependents[target].add(module)
                for parent in _ancestor_packages(target):
                    dependents[parent].add(module)
    return dependents


def _ancestor_packages(module: str) -> list[str]:
    parts = module.split(".")
    return [".".join(parts[:i]) for i in range(2, len(parts))]


def _impacted_modules(changed: set[str], dependents: dict[str, set[str]]) -> set[str]:
    impacted = set(changed)
    frontier = list(changed)
    while frontier:
        for dependent in dependents.get(frontier.pop(), ()):
            if dependent not in impacted:
                impacted.add(dependent)
                frontier.append(dependent)
    return impacted


def _changed_files(base: str) -> list[str]:
    tracked = subprocess.run(
        ["git", "diff", "--name-only", base], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    return sorted(set(tracked) | set(untracked))


def _changed_package_modules(changed_files: list[str], modules: dict[str, Path]) -> set[str]:
    by_path = {path.relative_to(REPO).as_posix(): module for module, path in modules.items()}
    return {by_path[f] for f in changed_files if f in by_path}


def _uses_example_projects(path: Path) -> bool:
    return "examples" in path.read_text(encoding="utf-8")


def select_tests(changed_files: list[str]) -> list[str]:
    test_files = sorted(p for p in TEST_DIR.glob("test_*.py"))
    if any(f in FULL_SUITE_TRIGGERS for f in changed_files):
        return [p.relative_to(REPO).as_posix() for p in test_files]
    modules = _package_modules()
    impacted = _impacted_modules(_changed_package_modules(changed_files, modules), _reverse_dependencies(modules))
    examples_changed = any(f.startswith(("examples/", "leanc/data/")) for f in changed_files)
    selected = set(ALWAYS_RUN)
    for test in test_files:
        rel = test.relative_to(REPO).as_posix()
        targets = {_resolve(name, modules) for name in _imported_modules(test)}
        if rel in changed_files or targets & impacted or (examples_changed and _uses_example_projects(test)):
            selected.add(rel)
    return sorted(selected)


def main() -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--base", default="HEAD", help="git revision to diff against (default: HEAD)")
    parser.add_argument("--run", action="store_true", help="run pytest on the selection")
    args = parser.parse_args()
    selection = select_tests(_changed_files(args.base))
    print("\n".join(selection))
    if not args.run:
        return 0
    return subprocess.run([sys.executable, "-m", "pytest", "-q", *selection], cwd=REPO, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
