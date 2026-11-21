from __future__ import annotations

import ast
import io
import tokenize
from collections.abc import Iterator
from pathlib import Path

from scripts.ruleset.core import Constraints, Violation
from scripts.ruleset.sources import checked_files, package_files, parse, rel

SHEBANG = "#!"
SHEBANG_POSITION = (1, 0)
BARE_EXCEPT = "<bare>"
MODULE_SCOPE = "<module>"
QUALNAME_SEPARATOR = "."
SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def comment_tokens(text: str) -> list[tokenize.TokenInfo]:
    return [
        t
        for t in tokenize.generate_tokens(io.StringIO(text).readline)
        if t.type == tokenize.COMMENT and not (t.start == SHEBANG_POSITION and t.string.startswith(SHEBANG))
    ]


def check_no_comments(c: Constraints) -> Iterator[Violation]:
    for path in checked_files(c):
        for token in comment_tokens(path.read_text(encoding="utf-8")):
            yield Violation("no-comments", rel(c, path), token.start[0], token.string[:60])


def docstring_nodes(tree: ast.Module) -> Iterator[tuple[ast.AST, ast.Expr]]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None:
                yield node, node.body[0]


def check_no_docstrings(c: Constraints) -> Iterator[Violation]:
    for path in checked_files(c):
        for _, expr in docstring_nodes(parse(path)):
            yield Violation("no-docstrings", rel(c, path), expr.lineno, "docstring")


def check_max_file_lines(c: Constraints) -> Iterator[Violation]:
    for path in checked_files(c):
        count = len(path.read_text(encoding="utf-8").splitlines())
        if count > c.max_file_lines:
            yield Violation("max-file-lines", rel(c, path), count, f"{count} lines > {c.max_file_lines}")


def _handler_types(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return {BARE_EXCEPT}
    elements = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {ast.unparse(element).rpartition(".")[2] for element in elements}


def _is_silent(statement: ast.stmt) -> bool:
    if isinstance(statement, (ast.Pass, ast.Continue, ast.Break)):
        return True
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
        return statement.value.value is Ellipsis or isinstance(statement.value.value, str)
    return isinstance(statement, ast.Return) and (
        statement.value is None or (isinstance(statement.value, ast.Constant) and statement.value.value is None)
    )


def _scoped_handlers(node: ast.AST, scope: tuple[str, ...]) -> Iterator[tuple[ast.ExceptHandler, str]]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ExceptHandler):
            yield child, QUALNAME_SEPARATOR.join(scope) or MODULE_SCOPE
        inner = (*scope, child.name) if isinstance(child, SCOPE_NODES) else scope
        yield from _scoped_handlers(child, inner)


def handlers(path: Path) -> Iterator[tuple[ast.ExceptHandler, str]]:
    return _scoped_handlers(parse(path), ())


def check_no_catch_all(c: Constraints) -> Iterator[Violation]:
    forbidden = c.catch_all_types | {BARE_EXCEPT}
    entry_file, entry_function = c.catch_all_entry_point
    for path in package_files(c):
        path_rel = rel(c, path)
        for handler, qualname in handlers(path):
            types = _handler_types(handler)
            if not types & forbidden:
                continue
            if (path_rel, qualname) == (entry_file, entry_function) and BARE_EXCEPT not in types:
                continue
            yield Violation("no-catch-all", path_rel, handler.lineno, f"except {', '.join(sorted(types))} in {qualname}")


def check_no_swallow(c: Constraints) -> Iterator[Violation]:
    for path in package_files(c):
        for handler, qualname in handlers(path):
            types = _handler_types(handler)
            if all(_is_silent(s) for s in handler.body) and not types <= c.swallow_allowed:
                yield Violation(
                    "no-swallow", rel(c, path), handler.lineno, f"except {', '.join(sorted(types))} swallowed in {qualname}"
                )
