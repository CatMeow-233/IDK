"""Frama-C console output: goal totals, warnings, errors, cache hits and the facts WP warnings reveal."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

SMOKE_TESTS_CATEGORY = "Smoke Tests"
GOALS_DISCHARGED_WITHOUT_PROVER = ("Terminating", "Unreachable")
NO_GOALS_MARKER = "No goal generated"
WARNING_MARKER = "Warning:"
CACHE_MARKER = "[Cache]"
ERROR_MARKERS = (
    "User Error",
    "Frama-C aborted",
    "aborted:",
    "Fatal error",
    "Unexpected error",
    "fatal error",
    "Internal error",
)
MAX_MEMORY_MODEL_HYPOTHESES = 20
MAX_HYPOTHESIS_CHARS = 300

_PROVED_RE = re.compile(r"^\[wp\] Proved goals:\s*(\d+)\s*/\s*(\d+)\s*$", re.M)
_CATEGORY_RE = re.compile(r"^\s+([A-Za-z][\w .:-]*?)\s*:\s*(\d+)(?:\s*/\s*(\d+))?(?:\s*\(.*\))?\s*$")
_MESSAGE_RE = re.compile(r"^\[([\w:.-]+)\]\s?(.*)$")
_CONTINUATION_INDENT = "  "
_CACHE_HIT_RE = re.compile(r"\bfound\s*:\s*[1-9]")
_CONTRACT_MERGE_RE = re.compile(r"[^\n]*\n?[^\n]*found two contracts[^\n]*", re.I)
_MISSING_SPEC_RE = re.compile(r"Neither code nor specification for function (\w+)")
_GENERATED_CLAUSES_RE = re.compile(r"Neither code nor explicit [\w ,]+? for function (\w+)")
_MEMORY_MODEL_HYPOTHESIS_RE = re.compile(r"Memory model hypotheses for function '(\w+)':\s*(.*)$")


@dataclass
class ConsoleSummary:
    proved: int | None = None
    total: int | None = None
    categories: dict[str, int] = field(default_factory=dict)
    smoke: tuple[int, int] | None = None
    cached: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    no_goals: bool = False

    def goals_discharged_without_prover(self) -> int:
        return sum(self.categories.get(category, 0) for category in GOALS_DISCHARGED_WITHOUT_PROVER)


@dataclass
class WarningFacts:
    memory_model_hypotheses: list[str] = field(default_factory=list)
    missing_specification: list[str] = field(default_factory=list)
    generated_clauses: list[str] = field(default_factory=list)


def parse_console(text: str) -> ConsoleSummary:
    """Totals, warnings and errors from Frama-C's console output."""
    summary = ConsoleSummary()
    lines = text.splitlines()
    _read_last_totals(text, lines, summary)
    for message in _messages(lines):
        _record_message(message, summary)
    return summary


def _read_last_totals(text: str, lines: list[str], summary: ConsoleSummary) -> None:
    totals = list(_PROVED_RE.finditer(text))
    if not totals:
        return
    last = totals[-1]
    summary.proved, summary.total = int(last.group(1)), int(last.group(2))
    first_category_line = text[: last.end()].count("\n") + 1
    for line in lines[first_category_line:]:
        category = _CATEGORY_RE.match(line)
        if not category:
            break
        name = " ".join(category.group(1).split())
        if name == SMOKE_TESTS_CATEGORY and category.group(3) is not None:
            summary.smoke = (int(category.group(2)), int(category.group(3)))
        else:
            summary.categories[name] = int(category.group(2))


def _messages(lines: list[str]) -> Iterator[str]:
    block: list[str] | None = None
    for line in lines:
        if _MESSAGE_RE.match(line):
            if block is not None:
                yield _joined(block)
            block = [line]
        elif block is not None and line.startswith(_CONTINUATION_INDENT):
            block.append(line)
        elif block is not None:
            yield _joined(block)
            block = None
    if block is not None:
        yield _joined(block)


def _joined(block: list[str]) -> str:
    return " ".join(" ".join(block).split())


def _is_cache_hit(message: str) -> bool:
    return CACHE_MARKER in message and _CACHE_HIT_RE.search(message) is not None


def _record_message(message: str, summary: ConsoleSummary) -> None:
    if _is_cache_hit(message):
        summary.cached.append(message)
    if WARNING_MARKER in message:
        summary.warnings.append(message)
    if any(marker in message for marker in ERROR_MARKERS):
        summary.errors.append(message)
    if NO_GOALS_MARKER in message:
        summary.no_goals = True


def contract_merge_warning(console_text: str) -> str | None:
    """Frama-C's report that it merged a second ACSL contract into an existing one, if it made one."""
    merge = _CONTRACT_MERGE_RE.search(console_text)
    return merge.group(0) if merge else None


def warning_facts(warnings: list[str]) -> WarningFacts:
    """Memory-model hypotheses and functions lacking code or specification, as WP warnings name them."""
    facts = WarningFacts()
    for warning in warnings:
        hypothesis = _MEMORY_MODEL_HYPOTHESIS_RE.search(warning)
        if hypothesis and len(facts.memory_model_hypotheses) < MAX_MEMORY_MODEL_HYPOTHESES:
            facts.memory_model_hypotheses.append(
                f"{hypothesis.group(1)}: {hypothesis.group(2)[:MAX_HYPOTHESIS_CHARS]}"
            )
        missing = _MISSING_SPEC_RE.search(warning)
        if missing and missing.group(1) not in facts.missing_specification:
            facts.missing_specification.append(missing.group(1))
        generated = _GENERATED_CLAUSES_RE.search(warning)
        if generated and generated.group(1) not in facts.generated_clauses:
            facts.generated_clauses.append(generated.group(1))
    return facts
