from __future__ import annotations

import argparse
import ast
import io
import json
import sys
import tokenize
import tomllib
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONSTRAINTS_FILE = "constraints.toml"
SHEBANG = "#!"
BARE_EXCEPT = "<bare>"
MODULE_SCOPE = "<module>"
DESCRIPTION = "check (and --fix) the repository constraints declared in constraints.toml"

SCHEMA: dict[str, dict[str, type]] = {
    "package": {"name": str, "python_trees": list, "excluded_dirs": list},
    "source": {"max_file_lines": int},
    "exceptions": {"catch_all_types": list, "catch_all_entry_point": str, "swallow_allowed": list},
    "directives": {"file": str, "layer_block_begin": str, "layer_block_end": str},
}
DYNAMIC_SECTIONS = ("layers",)


class ConstraintsError(Exception):
    pass


@dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    line: int
    message: str

    def render(self) -> str:
        return f"{self.rule} {self.path}:{self.line} {self.message}"


@dataclass(frozen=True)
class Constraints:
    root: Path
    package: str
    python_trees: tuple[str, ...]
    excluded_dirs: frozenset[str]
    layers: dict[str, tuple[str, ...]]
    max_file_lines: int
    catch_all_types: frozenset[str]
    catch_all_entry_point: tuple[str, str]
    swallow_allowed: frozenset[str]
    directives_file: str
    layer_block_begin: str
    layer_block_end: str


def _require_section(data: dict, section: str) -> dict:
    value = data.get(section)
    if not isinstance(value, dict):
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: missing table [{section}]")
    return value


def _validate_schema(data: dict) -> None:
    unknown_sections = set(data) - set(SCHEMA) - set(DYNAMIC_SECTIONS)
    if unknown_sections:
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: unknown tables {sorted(unknown_sections)}")
    for section, keys in SCHEMA.items():
        table = _require_section(data, section)
        if set(table) != set(keys):
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE}: [{section}] keys must be exactly {sorted(keys)}, got {sorted(table)}"
            )
        for key, expected in keys.items():
            if not isinstance(table[key], expected) or isinstance(table[key], bool):
                raise ConstraintsError(f"{CONSTRAINTS_FILE}: [{section}].{key} must be {expected.__name__}")


def _validated_layers(data: dict) -> dict[str, tuple[str, ...]]:
    table = _require_section(data, "layers")
    layers: dict[str, tuple[str, ...]] = {}
    for layer, deps in table.items():
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise ConstraintsError(f"{CONSTRAINTS_FILE}: [layers].{layer} must be a list of layer names")
        if layer in deps:
            raise ConstraintsError(f"{CONSTRAINTS_FILE}: layer {layer!r} depends on itself")
        if len(set(deps)) != len(deps):
            raise ConstraintsError(f"{CONSTRAINTS_FILE}: [layers].{layer} lists a layer twice")
        layers[layer] = tuple(deps)
    undeclared = sorted({d for deps in layers.values() for d in deps} - set(layers))
    if undeclared:
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: [layers] references undeclared layers {undeclared}")
    layer_ranks(layers)
    return layers


def _entry_point(raw: str) -> tuple[str, str]:
    path, sep, function = raw.partition(":")
    if not sep or not path or not function:
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: catch_all_entry_point must be 'path.py:function', got {raw!r}")
    return path, function


def load_constraints(root: Path = REPO) -> Constraints:
    path = root / CONSTRAINTS_FILE
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    _validate_schema(data)
    package, source, exceptions, directives = (data[s] for s in ("package", "source", "exceptions", "directives"))
    return Constraints(
        root=root,
        package=package["name"],
        python_trees=tuple(package["python_trees"]),
        excluded_dirs=frozenset(package["excluded_dirs"]),
        layers=_validated_layers(data),
        max_file_lines=source["max_file_lines"],
        catch_all_types=frozenset(exceptions["catch_all_types"]),
        catch_all_entry_point=_entry_point(exceptions["catch_all_entry_point"]),
        swallow_allowed=frozenset(exceptions["swallow_allowed"]),
        directives_file=directives["file"],
        layer_block_begin=directives["layer_block_begin"],
        layer_block_end=directives["layer_block_end"],
    )


def layer_ranks(layers: dict[str, tuple[str, ...]]) -> dict[str, int]:
    ranks: dict[str, int] = {}
    visiting: set[str] = set()

    def rank(layer: str) -> int:
        if layer in ranks:
            return ranks[layer]
        if layer in visiting:
            raise ConstraintsError(f"{CONSTRAINTS_FILE}: [layers] dependency cycle through {layer!r}")
        visiting.add(layer)
        ranks[layer] = 1 + max((rank(dep) for dep in layers[layer]), default=-1)
        visiting.discard(layer)
        return ranks[layer]

    for layer in sorted(layers):
        rank(layer)
    return ranks


def _python_files(c: Constraints, tree: str) -> list[Path]:
    return sorted(p for p in (c.root / tree).rglob("*.py") if not set(p.relative_to(c.root).parts) & c.excluded_dirs)


def _checked_files(c: Constraints) -> list[Path]:
    return [p for tree in c.python_trees for p in _python_files(c, tree)]


def _rel(c: Constraints, path: Path) -> str:
    return path.relative_to(c.root).as_posix()


def _module_name(c: Constraints, path: Path) -> str:
    parts = list(path.relative_to(c.root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _layer_of(c: Constraints, module: str) -> str | None:
    parts = module.split(".")
    return parts[1] if parts[0] == c.package and len(parts) > 1 else None


def _package_modules(c: Constraints) -> dict[str, Path]:
    return {_module_name(c, p): p for p in _python_files(c, c.package)}


def _package_imports(c: Constraints, tree: ast.Module) -> Iterator[tuple[str, int, bool]]:
    top_level = {id(node) for node in tree.body}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [f"{node.module}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        for name in names:
            if name == c.package or name.startswith(c.package + "."):
                yield name, node.lineno, id(node) in top_level


def _resolve(imported: str, modules: dict[str, Path]) -> str | None:
    candidate = imported
    while candidate:
        if candidate in modules:
            return candidate
        candidate = candidate.rpartition(".")[0]
    return None


def check_layer_known(c: Constraints) -> Iterator[Violation]:
    for module, path in _package_modules(c).items():
        layer = _layer_of(c, module)
        if layer is not None and layer not in c.layers:
            yield Violation("layer-known", _rel(c, path), 1, f"module {module} is outside the declared layers")


def check_layer_dependency(c: Constraints) -> Iterator[Violation]:
    modules = _package_modules(c)
    for module, path in modules.items():
        source = _layer_of(c, module)
        if source is None or source not in c.layers:
            continue
        for imported, line, _ in _package_imports(c, ast.parse(path.read_text(encoding="utf-8"))):
            target = _layer_of(c, _resolve(imported, modules) or imported)
            if target is not None and target != source and target not in c.layers[source]:
                yield Violation("layer-dependency", _rel(c, path), line, f"{source} -> {target} ({imported})")


def check_import_top_level(c: Constraints) -> Iterator[Violation]:
    for path in _python_files(c, c.package):
        for imported, line, top in _package_imports(c, ast.parse(path.read_text(encoding="utf-8"))):
            if not top:
                yield Violation("import-top-level", _rel(c, path), line, f"function-level import of {imported}")


def _import_graph(c: Constraints) -> dict[str, set[str]]:
    modules = _package_modules(c)
    graph: dict[str, set[str]] = defaultdict(set)
    for module, path in modules.items():
        for imported, _, _ in _package_imports(c, ast.parse(path.read_text(encoding="utf-8"))):
            target = _resolve(imported, modules)
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
    modules = _package_modules(c)
    for cycle in _cycles(_import_graph(c), sorted(modules)):
        yield Violation("import-cycle", _rel(c, modules[cycle[0]]), 1, " -> ".join(cycle))


def _comment_tokens(text: str) -> list[tokenize.TokenInfo]:
    return [
        t
        for t in tokenize.generate_tokens(io.StringIO(text).readline)
        if t.type == tokenize.COMMENT and not (t.start[0] == 1 and t.string.startswith(SHEBANG))
    ]


def check_no_comments(c: Constraints) -> Iterator[Violation]:
    for path in _checked_files(c):
        for token in _comment_tokens(path.read_text(encoding="utf-8")):
            yield Violation("no-comments", _rel(c, path), token.start[0], token.string[:60])


def _docstring_nodes(tree: ast.Module) -> Iterator[tuple[ast.AST, ast.Expr]]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None:
                yield node, node.body[0]


def check_no_docstrings(c: Constraints) -> Iterator[Violation]:
    for path in _checked_files(c):
        for _, expr in _docstring_nodes(ast.parse(path.read_text(encoding="utf-8"))):
            yield Violation("no-docstrings", _rel(c, path), expr.lineno, "docstring")


def check_max_file_lines(c: Constraints) -> Iterator[Violation]:
    for path in _checked_files(c):
        count = len(path.read_text(encoding="utf-8").splitlines())
        if count > c.max_file_lines:
            yield Violation("max-file-lines", _rel(c, path), count, f"{count} lines > {c.max_file_lines}")


def _handler_types(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return {BARE_EXCEPT}
    elements = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {ast.unparse(element).rpartition(".")[2] for element in elements}


def _is_silent(statement: ast.stmt) -> bool:
    if isinstance(statement, (ast.Pass, ast.Continue)):
        return True
    return isinstance(statement, ast.Return) and (
        statement.value is None or (isinstance(statement.value, ast.Constant) and statement.value.value is None)
    )


def _handlers(path: Path) -> Iterator[tuple[ast.ExceptHandler, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner: dict[int, str] = {}
    for function in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        for inner in ast.walk(function):
            owner[id(inner)] = function.name
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            yield node, owner.get(id(node), MODULE_SCOPE)


def check_no_catch_all(c: Constraints) -> Iterator[Violation]:
    forbidden = c.catch_all_types | {BARE_EXCEPT}
    for path in _python_files(c, c.package):
        rel = _rel(c, path)
        for handler, function in _handlers(path):
            types = _handler_types(handler)
            if not types & forbidden:
                continue
            if (rel, function) == c.catch_all_entry_point and BARE_EXCEPT not in types:
                continue
            yield Violation("no-catch-all", rel, handler.lineno, f"except {', '.join(sorted(types))} in {function}()")


def check_no_swallow(c: Constraints) -> Iterator[Violation]:
    for path in _python_files(c, c.package):
        for handler, function in _handlers(path):
            types = _handler_types(handler)
            if all(_is_silent(s) for s in handler.body) and not types <= c.swallow_allowed:
                yield Violation(
                    "no-swallow", _rel(c, path), handler.lineno, f"except {', '.join(sorted(types))} swallowed in {function}()"
                )


def expected_layer_block(c: Constraints) -> list[str]:
    ranks = layer_ranks(c.layers)
    by_rank: dict[int, list[str]] = defaultdict(list)
    for layer in c.layers:
        by_rank[ranks[layer]].append(layer)
    order = " < ".join(", ".join(by_rank[r]) for r in sorted(by_rank))
    deps = [f"- {layer}: {' '.join(c.layers[layer]) or 'none'}" for layer in c.layers]
    return [c.layer_block_begin, order, *deps, c.layer_block_end]


def _layer_block_bounds(c: Constraints, lines: list[str]) -> tuple[int, int] | None:
    if lines.count(c.layer_block_begin) != 1 or lines.count(c.layer_block_end) != 1:
        return None
    begin, end = lines.index(c.layer_block_begin), lines.index(c.layer_block_end)
    return (begin, end) if begin < end else None


def check_directives_sync(c: Constraints) -> Iterator[Violation]:
    path = c.root / c.directives_file
    lines = path.read_text(encoding="utf-8").splitlines()
    bounds = _layer_block_bounds(c, lines)
    if bounds is None:
        yield Violation("directives-sync", c.directives_file, 1, f"needs exactly one {c.layer_block_begin} ... {c.layer_block_end} block")
        return
    begin, end = bounds
    if lines[begin : end + 1] != expected_layer_block(c):
        yield Violation("directives-sync", c.directives_file, begin + 1, f"layer block differs from {CONSTRAINTS_FILE}")


RULES: dict[str, Callable[[Constraints], Iterator[Violation]]] = {
    "layer-known": check_layer_known,
    "layer-dependency": check_layer_dependency,
    "import-top-level": check_import_top_level,
    "import-cycle": check_import_cycle,
    "no-comments": check_no_comments,
    "no-docstrings": check_no_docstrings,
    "max-file-lines": check_max_file_lines,
    "no-catch-all": check_no_catch_all,
    "no-swallow": check_no_swallow,
    "directives-sync": check_directives_sync,
}


def check(c: Constraints, rules: list[str] | None = None) -> list[Violation]:
    return [v for rule in (rules or list(RULES)) for v in RULES[rule](c)]


def _without_comments(text: str) -> str:
    lines = text.splitlines(keepends=True)
    for token in reversed(_comment_tokens(text)):
        row, col = token.start
        line = lines[row - 1]
        newline = "\n" if line.endswith("\n") else ""
        kept = line[:col].rstrip()
        lines[row - 1] = kept + newline if kept else ""
    return "".join(lines)


def _strip_docstrings(tree: ast.Module) -> ast.Module:
    for node, expr in list(_docstring_nodes(tree)):
        node.body.remove(expr)
    return tree


def _docstring_line_ranges(text: str) -> list[tuple[int, int]] | None:
    tree = ast.parse(text)
    lines = text.splitlines()
    ranges = []
    for node, expr in _docstring_nodes(tree):
        if len(node.body) == 1:
            return None
        follower = node.body[1]
        starts_its_line = not lines[expr.lineno - 1][: expr.col_offset].strip()
        if follower.lineno <= expr.end_lineno or not starts_its_line:
            return None
        ranges.append((expr.lineno, expr.end_lineno))
    return ranges


def _without_docstrings(text: str) -> str | None:
    ranges = _docstring_line_ranges(text)
    if ranges is None:
        return None
    lines = text.splitlines(keepends=True)
    for start, end in sorted(ranges, reverse=True):
        del lines[start - 1 : end]
    return "".join(lines)


def _same_code(original: str, fixed: str) -> bool:
    before = ast.dump(_strip_docstrings(ast.parse(original)))
    return before == ast.dump(ast.parse(fixed))


def fixed_source(text: str) -> str | None:
    candidate = _without_docstrings(_without_comments(text))
    if candidate is None or not _same_code(text, candidate):
        return None
    return candidate


def fix(c: Constraints) -> tuple[list[str], list[str]]:
    fixed, unfixable = [], []
    for path in sorted({c.root / v.path for v in check(c, ["no-comments", "no-docstrings"])}):
        text = path.read_text(encoding="utf-8")
        result = fixed_source(text)
        if result is None:
            unfixable.append(_rel(c, path))
        else:
            path.write_text(result, encoding="utf-8")
            fixed.append(_rel(c, path))
    if list(check_directives_sync(c)):
        fixed.append(_fix_layer_block(c))
    return fixed, unfixable


def _fix_layer_block(c: Constraints) -> str:
    path = c.root / c.directives_file
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    bounds = _layer_block_bounds(c, lines)
    if bounds is None:
        lines += ["", *expected_layer_block(c)]
    else:
        lines[bounds[0] : bounds[1] + 1] = expected_layer_block(c)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return c.directives_file


def _report(violations: list[Violation], fixed: list[str], unfixable: list[str], as_json: bool) -> None:
    if as_json:
        print(json.dumps({
            "ok": not violations,
            "violations": [asdict(v) for v in violations],
            "fixed": fixed,
            "unfixable": unfixable,
        }, indent=2))
        return
    for path in fixed:
        print(f"fixed {path}")
    for path in unfixable:
        print(f"unfixable {path} (code would change; edit by hand)")
    for violation in violations:
        print(violation.render())
    print(f"{len(violations)} violation(s)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--fix", action="store_true", help="remove comments/docstrings and rewrite the CLAUDE.md layer block")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--root", type=Path, default=REPO, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        c = load_constraints(args.root)
    except (ConstraintsError, tomllib.TOMLDecodeError, OSError) as exc:
        print(f"constraints: {exc}", file=sys.stderr)
        return 2
    fixed, unfixable = fix(c) if args.fix else ([], [])
    violations = check(c)
    _report(violations, fixed, unfixable, args.json)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
