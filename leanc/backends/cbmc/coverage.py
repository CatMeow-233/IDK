from __future__ import annotations

from typing import Any

from leanc.backends.counterexample.locations import location_from_cbmc
from leanc.foundation.models import Location

MAX_LINE_RANGE_SPAN = 100_000
SATISFIED_GOAL = "satisfied"


def _parse_line_numbers(line_spec: Any) -> set[int]:
    lines: set[int] = set()
    for part in str(line_spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            first, _, last = part.partition("-")
            if first.strip().isdigit() and last.strip().isdigit():
                low, high = int(first), int(last)
                if 0 <= high - low <= MAX_LINE_RANGE_SPAN:
                    lines.update(range(low, high + 1))
        elif part.isdigit():
            lines.add(int(part))
    return lines


def _goal_lines(goal: dict, file: str, location: Location | None) -> set[int]:
    lines: set[int] = set()
    basic_block_lines = goal.get("basicBlockLines")
    if isinstance(basic_block_lines, dict):
        per_file = basic_block_lines.get(file)
        if isinstance(per_file, dict):
            for line_spec in per_file.values():
                lines |= _parse_line_numbers(line_spec)
    if not lines and location is not None and location.line is not None:
        lines.add(location.line)
    return lines


def coverage_by_file(goals: list[dict], files: list[str]) -> dict[str, dict]:
    stats: dict[str, dict] = {file: {"goals": 0, "covered": 0, "uncovered_lines": []} for file in files}
    covered_lines: dict[str, set[int]] = {file: set() for file in files}
    uncovered_lines: dict[str, set[int]] = {file: set() for file in files}
    for goal in goals:
        location = location_from_cbmc(goal.get("sourceLocation"))
        file = location.file if location else None
        if file not in stats:
            continue
        lines = _goal_lines(goal, file, location)
        stats[file]["goals"] += 1
        if str(goal.get("status", "")).lower() == SATISFIED_GOAL:
            stats[file]["covered"] += 1
            covered_lines[file] |= lines
        else:
            uncovered_lines[file] |= lines
    for file in files:
        stats[file]["uncovered_lines"] = sorted(uncovered_lines[file] - covered_lines[file])
    return stats
