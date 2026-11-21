from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CONSTRAINTS_FILE = "constraints.toml"


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

    @property
    def package_dir(self) -> Path:
        return self.root / self.package
