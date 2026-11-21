from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator

from scripts.ruleset.config import layer_ranks
from scripts.ruleset.core import CONSTRAINTS_FILE, Constraints, Violation

LAYER_ORDER_SEPARATOR = " < "
NO_DEPENDENCIES = "none"


def expected_layer_block(c: Constraints) -> list[str]:
    ranks = layer_ranks(c.layers)
    by_rank: dict[int, list[str]] = defaultdict(list)
    for layer in c.layers:
        by_rank[ranks[layer]].append(layer)
    order = LAYER_ORDER_SEPARATOR.join(", ".join(by_rank[r]) for r in sorted(by_rank))
    deps = [f"- {layer}: {' '.join(c.layers[layer]) or NO_DEPENDENCIES}" for layer in c.layers]
    return [c.layer_block_begin, order, *deps, c.layer_block_end]


def _layer_block_bounds(c: Constraints, lines: list[str]) -> tuple[int, int] | None:
    if lines.count(c.layer_block_begin) != 1 or lines.count(c.layer_block_end) != 1:
        return None
    begin, end = lines.index(c.layer_block_begin), lines.index(c.layer_block_end)
    return (begin, end) if begin < end else None


def check_directives_sync(c: Constraints) -> Iterator[Violation]:
    lines = (c.root / c.directives_file).read_text(encoding="utf-8").splitlines()
    bounds = _layer_block_bounds(c, lines)
    if bounds is None:
        yield Violation(
            "directives-sync", c.directives_file, 1, f"needs exactly one {c.layer_block_begin} ... {c.layer_block_end} block"
        )
        return
    begin, end = bounds
    if lines[begin : end + 1] != expected_layer_block(c):
        yield Violation("directives-sync", c.directives_file, begin + 1, f"layer block differs from {CONSTRAINTS_FILE}")


def rewrite_layer_block(c: Constraints) -> None:
    path = c.root / c.directives_file
    lines = path.read_text(encoding="utf-8").splitlines()
    bounds = _layer_block_bounds(c, lines)
    if bounds is None:
        lines += ["", *expected_layer_block(c)]
    else:
        lines[bounds[0] : bounds[1] + 1] = expected_layer_block(c)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
