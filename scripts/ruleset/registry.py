from __future__ import annotations

from collections.abc import Callable, Iterator

from scripts.ruleset.code_rules import (
    check_max_file_lines,
    check_no_catch_all,
    check_no_comments,
    check_no_docstrings,
    check_no_swallow,
)
from scripts.ruleset.core import Constraints, Violation
from scripts.ruleset.directives import check_directives_sync
from scripts.ruleset.import_rules import (
    check_import_absolute,
    check_import_cycle,
    check_import_top_level,
    check_layer_dependency,
    check_layer_known,
    check_root_module,
)

RULES: dict[str, Callable[[Constraints], Iterator[Violation]]] = {
    "layer-known": check_layer_known,
    "root-module": check_root_module,
    "layer-dependency": check_layer_dependency,
    "import-top-level": check_import_top_level,
    "import-absolute": check_import_absolute,
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
