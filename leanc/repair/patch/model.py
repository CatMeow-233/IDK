from __future__ import annotations

from dataclasses import dataclass, field

CONTEXT = " "
REMOVED = "-"
ADDED = "+"
OLD_SIDE_OPS = (CONTEXT, REMOVED)
NEW_SIDE_OPS = (CONTEXT, ADDED)
LINE_BREAK = "\n"


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[str, str]] = field(default_factory=list)
    section: str = ""

    def old_lines(self) -> list[str]:
        return [text for op, text in self.lines if op in OLD_SIDE_OPS]

    def new_lines(self) -> list[str]:
        return [text for op, text in self.lines if op in NEW_SIDE_OPS]

    def header(self) -> str:
        return f"@@ -{self.old_start},{self.old_count} +{self.new_start},{self.new_count} @@"


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)
    rename_or_copy: bool = False
    binary: bool = False
    mode_change: bool = False
    git_new_file: bool = False
    git_deleted_file: bool = False

    @property
    def path(self) -> str:
        written = self.new_path if self.new_path is not None else self.old_path
        assert written is not None, "parse_unified_diff never yields /dev/null on both sides"
        return written

    @property
    def is_new_file(self) -> bool:
        return self.old_path is None

    @property
    def is_deletion(self) -> bool:
        return self.new_path is None


def split_lines(text: str) -> list[str]:
    if not text:
        return []
    parts = text.split(LINE_BREAK)
    lines = [part + LINE_BREAK for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines
