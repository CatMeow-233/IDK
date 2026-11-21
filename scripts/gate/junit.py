from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path

from scripts.gate.model import StepResult

SUITE_TAG = "testsuite"
COUNTED_ATTRIBUTES = ("tests", "failures", "errors", "skipped")


@dataclass(frozen=True)
class JunitCounts:
    tests: int
    failures: int
    errors: int
    skipped: int


def read_junit(path: Path) -> JunitCounts | str:
    if not path.is_file():
        return f"pytest wrote no junit report at {path}"
    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError as exc:
        return f"unreadable junit report {path}: {exc}"
    suites = [root] if root.tag == SUITE_TAG else root.findall(SUITE_TAG)
    if not suites:
        return f"junit report {path} has no {SUITE_TAG} element"
    totals = {name: sum(int(suite.get(name, "0")) for suite in suites) for name in COUNTED_ATTRIBUTES}
    return JunitCounts(**totals)


def judge_junit(counts: JunitCounts | str) -> StepResult:
    if isinstance(counts, str):
        return StepResult(False, counts)
    summary = f"{counts.tests} tests, {counts.failures} failed, {counts.errors} errors, {counts.skipped} skipped"
    if counts.tests == 0:
        return StepResult(False, f"no tests ran ({summary})")
    if counts.failures or counts.errors or counts.skipped:
        return StepResult(False, f"every test must run and pass; skipped means a required tool is missing ({summary})")
    return StepResult(True, summary)
