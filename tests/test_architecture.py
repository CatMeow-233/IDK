"""Architecture rules: strict layering, no import cycles, no comments, one-line docstrings."""

from __future__ import annotations

import ast
import io
import tokenize
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "leanc"
CHECKED_TREES = (PACKAGE, REPO / "tests", REPO / "scripts")

ALLOWED_DEPENDENCIES: dict[str, frozenset[str]] = {
    "foundation": frozenset(),
    "specs": frozenset({"foundation"}),
    "lang": frozenset({"foundation"}),
    "rules": frozenset({"foundation", "specs", "lang"}),
    "toolchain": frozenset({"foundation", "specs"}),
    "reports": frozenset({"foundation"}),
    "llm": frozenset({"foundation", "specs"}),
    "backends": frozenset({"foundation", "specs", "lang", "rules", "toolchain"}),
    "repair": frozenset({"foundation", "specs", "lang", "rules"}),
    "pipeline": frozenset(
        {"foundation", "specs", "lang", "rules", "toolchain", "reports", "llm", "backends", "repair"}
    ),
    "cli": frozenset(
        {"foundation", "specs", "lang", "rules", "toolchain", "reports", "llm", "backends", "repair", "pipeline"}
    ),
    "__main__": frozenset({"cli"}),
}
ROOT_MODULE = "leanc"


def _python_files(tree: Path) -> list[Path]:
    return sorted(p for p in tree.rglob("*.py") if "fixtures" not in p.parts)


def _module_name(path: Path) -> str:
    rel = path.relative_to(REPO).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _layer_of(module: str) -> str | None:
    parts = module.split(".")
    if parts[0] != ROOT_MODULE or len(parts) == 1:
        return None
    return parts[1]


def _leanc_imports(path: Path) -> list[tuple[str, int, bool]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    top_level = {id(node) for node in tree.body}
    found: list[tuple[str, int, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        for name in names:
            if name == ROOT_MODULE or name.startswith(ROOT_MODULE + "."):
                found.append((name, node.lineno, id(node) in top_level))
    return found


def _package_modules() -> dict[str, Path]:
    return {_module_name(p): p for p in _python_files(PACKAGE)}


def test_every_module_belongs_to_a_known_layer():
    unknown = sorted(
        m for m in _package_modules() if _layer_of(m) is not None and _layer_of(m) not in ALLOWED_DEPENDENCIES
    )
    assert not unknown, f"modules outside the declared layers: {unknown}"


def test_layer_dependencies_follow_the_architecture():
    violations = []
    for module, path in _package_modules().items():
        source_layer = _layer_of(module)
        if source_layer is None:
            continue
        for imported, line, _ in _leanc_imports(path):
            target_layer = _layer_of(imported)
            if target_layer is None or target_layer == source_layer:
                continue
            if target_layer not in ALLOWED_DEPENDENCIES[source_layer]:
                violations.append(f"{path.relative_to(REPO)}:{line}: {source_layer} -> {target_layer} ({imported})")
    assert not violations, "forbidden layer dependencies:\n" + "\n".join(violations)


def test_imports_are_at_module_top_level():
    lazy = [
        f"{path.relative_to(REPO)}:{line}: {imported}"
        for path in _python_files(PACKAGE)
        for imported, line, top in _leanc_imports(path)
        if not top
    ]
    assert not lazy, "function-level leanc imports hide dependencies:\n" + "\n".join(lazy)


def _resolve(imported: str, modules: dict[str, Path]) -> str | None:
    candidate = imported
    while candidate:
        if candidate in modules:
            return candidate
        candidate = candidate.rpartition(".")[0]
    return None


def test_no_import_cycles_between_modules():
    modules = _package_modules()
    graph: dict[str, set[str]] = defaultdict(set)
    for module, path in modules.items():
        for imported, _, _ in _leanc_imports(path):
            target = _resolve(imported, modules)
            if target and target != module and target != ROOT_MODULE:
                graph[module].add(target)
    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[list[str]] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in sorted(graph[node]):
            if state.get(nxt) == 1:
                cycles.append(stack[stack.index(nxt):] + [nxt])
            elif nxt not in state:
                visit(nxt)
        stack.pop()
        state[node] = 2

    for module in sorted(modules):
        if module not in state:
            visit(module)
    assert not cycles, "import cycles:\n" + "\n".join(" -> ".join(c) for c in cycles)


def test_no_comments_in_python_sources():
    offenders = []
    for tree in CHECKED_TREES:
        for path in _python_files(tree):
            text = path.read_text(encoding="utf-8")
            for token in tokenize.generate_tokens(io.StringIO(text).readline):
                if token.type == tokenize.COMMENT and not (token.start[0] == 1 and token.string.startswith("#!")):
                    offenders.append(f"{path.relative_to(REPO)}:{token.start[0]}: {token.string[:60]}")
    assert not offenders, f"{len(offenders)} comment(s) found:\n" + "\n".join(offenders[:200])


def test_docstrings_are_single_line():
    offenders = []
    for tree in CHECKED_TREES:
        for path in _python_files(tree):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                doc = ast.get_docstring(node, clean=True)
                if doc is not None and "\n" in doc.strip():
                    offenders.append(f"{path.relative_to(REPO)}:{getattr(node, 'lineno', 1)}")
    assert not offenders, f"{len(offenders)} multi-line docstring(s):\n" + "\n".join(offenders[:200])
