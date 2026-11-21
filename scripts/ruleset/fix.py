from __future__ import annotations

import ast
import re

from scripts.ruleset.code_rules import comment_tokens, docstring_nodes
from scripts.ruleset.core import Constraints
from scripts.ruleset.directives import check_directives_sync, rewrite_layer_block
from scripts.ruleset.registry import check
from scripts.ruleset.sources import rel

ENCODING_COOKIE_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")
ENCODING_COOKIE_LINES = 2
FIXABLE_RULES = ["no-comments", "no-docstrings"]


def _has_encoding_cookie(text: str) -> bool:
    return any(ENCODING_COOKIE_RE.match(line) for line in text.splitlines()[:ENCODING_COOKIE_LINES])


def _without_comments(text: str) -> str:
    lines = text.splitlines(keepends=True)
    for token in reversed(comment_tokens(text)):
        row, col = token.start
        line = lines[row - 1]
        newline = "\n" if line.endswith("\n") else ""
        kept = line[:col].rstrip()
        lines[row - 1] = kept + newline if kept else ""
    return "".join(lines)


def _strip_docstrings(tree: ast.Module) -> ast.Module:
    for node, expr in list(docstring_nodes(tree)):
        node.body.remove(expr)
    return tree


def _docstring_line_ranges(text: str) -> list[tuple[int, int]] | None:
    lines = text.splitlines()
    ranges = []
    for node, expr in docstring_nodes(ast.parse(text)):
        if len(node.body) == 1:
            return None
        starts_its_line = not lines[expr.lineno - 1][: expr.col_offset].strip()
        if node.body[1].lineno <= expr.end_lineno or not starts_its_line:
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
    return ast.dump(_strip_docstrings(ast.parse(original))) == ast.dump(ast.parse(fixed))


def fixed_source(text: str) -> str | None:
    if _has_encoding_cookie(text):
        return None
    candidate = _without_docstrings(_without_comments(text))
    if candidate is None or not _same_code(text, candidate):
        return None
    return candidate


def fix(c: Constraints) -> tuple[list[str], list[str]]:
    fixed, unfixable = [], []
    for path in sorted({c.root / v.path for v in check(c, FIXABLE_RULES)}):
        result = fixed_source(path.read_text(encoding="utf-8"))
        if result is None:
            unfixable.append(rel(c, path))
        else:
            path.write_text(result, encoding="utf-8")
            fixed.append(rel(c, path))
    if list(check_directives_sync(c)):
        rewrite_layer_block(c)
        fixed.append(c.directives_file)
    return fixed, unfixable
