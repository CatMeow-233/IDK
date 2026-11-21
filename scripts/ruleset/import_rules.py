from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Iterator

from scripts.ruleset.core import Constraints, Violation
from scripts.ruleset.sources import layer_of, package_files, package_imports, package_modules, parse, rel, resolve

DYNAMIC_IMPORT_CALLS = frozenset({"import_module", "__import__"})


def check_layer_known(c: Constraints) -> Iterator[Violation]:
    for module, path in package_modules(c).items():
        if module != c.package and layer_of(c, module) not in c.layers:
            yield Violation("layer-known", rel(c, path), 1, f"module {module} is outside the declared layers")


def check_root_module(c: Constraints) -> Iterator[Violation]:
    root_init = c.package_dir / "__init__.py"
    if not root_init.is_file():
        yield Violation("root-module", rel(c, c.package_dir), 1, f"{c.package}/__init__.py is required")
        return
    for site in package_imports(c, parse(root_init)):
        yield Violation("root-module", rel(c, root_init), site.line, f"root module imports {site.names[0]}")


def check_layer_dependency(c: Constraints) -> Iterator[Violation]:
    for module, path in package_modules(c).items():
        source = layer_of(c, module)
        if source not in c.layers:
            continue
        for site in package_imports(c, parse(path)):
            for target in sorted({layer_of(c, name) for name in site.names} - {None, source}):
                if target not in c.layers[source]:
                    yield Violation("layer-dependency", rel(c, path), site.line, f"{source} -> {target} ({site.names[0]})")


def check_import_top_level(c: Constraints) -> Iterator[Violation]:
    for path in package_files(c):
        for site in package_imports(c, parse(path)):
            if not site.top_level:
                yield Violation("import-top-level", rel(c, path), site.line, f"function-level import of {site.names[0]}")


def _dynamic_import_name(node: ast.Call) -> str | None:
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
    return name if name in DYNAMIC_IMPORT_CALLS else None


def check_import_absolute(c: Constraints) -> Iterator[Violation]:
    for path in package_files(c):
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.ImportFrom) and node.level > 0:
                yield Violation("import-absolute", rel(c, path), node.lineno, "relative import")
            elif isinstance(node, ast.Call) and (name := _dynamic_import_name(node)):
                yield Violation("import-absolute", rel(c, path), node.lineno, f"dynamic import via {name}()")


def _import_graph(c: Constraints) -> dict[str, set[str]]:
    modules = package_modules(c)
    graph: dict[str, set[str]] = defaultdict(set)
    for module, path in modules.items():
        for site in package_imports(c, parse(path)):
            for name in site.names:
                target = resolve(name, modules)
                if target and target not in (module, c.package):
                    graph[module].add(target)
    return graph


def _cycles(graph: dict[str, set[str]], nodes: list[str]) -> list[list[str]]:
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

    for node in nodes:
        if node not in state:
            visit(node)
    return cycles


def check_import_cycle(c: Constraints) -> Iterator[Violation]:
    modules = package_modules(c)
    for cycle in _cycles(_import_graph(c), sorted(modules)):
        yield Violation("import-cycle", rel(c, modules[cycle[0]]), 1, " -> ".join(cycle))
