from __future__ import annotations

import ast
import io
import tokenize
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "leanc"
CHECKED_TREES = (PACKAGE, REPO / "tests", REPO / "scripts")
MAX_FILE_LINES = 600

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


def test_modules_and_tests_stay_small():
    oversized = []
    for tree in CHECKED_TREES:
        for path in _python_files(tree):
            line_count = len(path.read_text(encoding="utf-8").splitlines())
            if line_count > MAX_FILE_LINES:
                oversized.append(f"{path.relative_to(REPO)}: {line_count} lines")
    assert not oversized, f"split by responsibility (> {MAX_FILE_LINES} lines):\n" + "\n".join(oversized)


def test_no_docstrings():
    offenders = []
    for tree in CHECKED_TREES:
        for path in _python_files(tree):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    if ast.get_docstring(node, clean=False) is not None:
                        offenders.append(f"{path.relative_to(REPO)}:{getattr(node, 'lineno', 1)}")
    assert not offenders, f"{len(offenders)} docstring(s):\n" + "\n".join(offenders[:200])


CATCH_ALL_TYPES = frozenset({"Exception", "BaseException"})
BENIGN_SWALLOWED_TYPES = frozenset(
    {"ProcessLookupError", "FileNotFoundError", "FileExistsError", "BlockingIOError", "BrokenPipeError"}
)
TOP_LEVEL_ENTRY_POINT = ("leanc/cli.py", "main")


def _handler_types(handler: ast.ExceptHandler) -> list[str]:
    if handler.type is None:
        return ["<bare>"]
    elements = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return [ast.unparse(element).rpartition(".")[2] for element in elements]


def _swallows(handler: ast.ExceptHandler) -> bool:
    def is_silent(statement: ast.stmt) -> bool:
        if isinstance(statement, (ast.Pass, ast.Continue)):
            return True
        return isinstance(statement, ast.Return) and (
            statement.value is None or (isinstance(statement.value, ast.Constant) and statement.value.value is None)
        )

    return all(is_silent(statement) for statement in handler.body)


def _enclosing_functions(tree: ast.Module) -> dict[int, str]:
    owner: dict[int, str] = {}
    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for function in functions:
        for inner in ast.walk(function):
            owner[id(inner)] = function.name
    return owner


def _handlers(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owners = _enclosing_functions(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            yield node, owners.get(id(node), "<module>")


def test_no_catch_all_exception_handlers():
    offenders = []
    for path in _python_files(PACKAGE):
        rel = str(path.relative_to(REPO))
        for handler, function in _handlers(path):
            types = set(_handler_types(handler))
            if not types & (CATCH_ALL_TYPES | {"<bare>"}):
                continue
            if (rel, function) == TOP_LEVEL_ENTRY_POINT and "<bare>" not in types:
                continue
            offenders.append(f"{rel}:{handler.lineno}: except {', '.join(sorted(types))} in {function}()")
    assert not offenders, "catch-all handlers hide bugs:\n" + "\n".join(offenders)


def test_exceptions_are_never_silently_swallowed():
    offenders = []
    for path in _python_files(PACKAGE):
        for handler, function in _handlers(path):
            types = set(_handler_types(handler))
            if _swallows(handler) and not types <= BENIGN_SWALLOWED_TYPES:
                offenders.append(
                    f"{path.relative_to(REPO)}:{handler.lineno}: except {', '.join(sorted(types))}: swallowed in {function}()"
                )
    assert not offenders, "silently swallowed exceptions:\n" + "\n".join(offenders)
