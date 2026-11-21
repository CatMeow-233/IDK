from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from scripts.ruleset.core import Constraints


@dataclass(frozen=True)
class ImportSite:
    line: int
    top_level: bool
    names: tuple[str, ...]


def package_files(c: Constraints) -> list[Path]:
    return sorted(c.package_dir.rglob("*.py"))


def _tree_files(c: Constraints, tree: str) -> list[Path]:
    if tree == c.package:
        return package_files(c)
    return sorted(p for p in (c.root / tree).rglob("*.py") if not set(p.relative_to(c.root).parts) & c.excluded_dirs)


def checked_files(c: Constraints) -> list[Path]:
    return [p for tree in c.python_trees for p in _tree_files(c, tree)]


def rel(c: Constraints, path: Path) -> str:
    return path.relative_to(c.root).as_posix()


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def module_name(c: Constraints, path: Path) -> str:
    parts = list(path.relative_to(c.root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def package_modules(c: Constraints) -> dict[str, Path]:
    return {module_name(c, p): p for p in package_files(c)}


def layer_of(c: Constraints, name: str) -> str | None:
    parts = name.split(".")
    if parts[0] != c.package or len(parts) < 2:
        return None
    candidate = parts[1]
    if (c.package_dir / candidate).is_dir() or (c.package_dir / f"{candidate}.py").is_file():
        return candidate
    return None


def _belongs_to_package(c: Constraints, name: str) -> bool:
    return name == c.package or name.startswith(c.package + ".")


def package_imports(c: Constraints, tree: ast.Module) -> Iterator[ImportSite]:
    top_level = {id(node) for node in tree.body}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = (node.module, *(f"{node.module}.{alias.name}" for alias in node.names))
        elif isinstance(node, ast.Import):
            names = tuple(alias.name for alias in node.names)
        else:
            continue
        own = tuple(n for n in names if _belongs_to_package(c, n))
        if own:
            yield ImportSite(node.lineno, id(node) in top_level, own)


def resolve(name: str, modules: dict[str, Path]) -> str | None:
    candidate = name
    while candidate:
        if candidate in modules:
            return candidate
        candidate = candidate.rpartition(".")[0]
    return None
