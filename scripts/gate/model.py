from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

Runner = Callable[[list[str], Path, Mapping[str, str]], int]


@dataclass(frozen=True)
class StepResult:
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class Step:
    name: str
    run: Callable[[], StepResult]


@dataclass
class GateState:
    head: str
    scratch: Path
    base: str = ""
    results: list[tuple[str, StepResult, float]] = field(default_factory=list)

    @property
    def worktree(self) -> Path:
        return self.scratch / "head"

    @property
    def base_worktree(self) -> Path:
        return self.scratch / "base"

    @property
    def junit_report(self) -> Path:
        return self.scratch / "junit.xml"
