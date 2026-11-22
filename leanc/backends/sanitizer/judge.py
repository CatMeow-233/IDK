from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import zip_longest

from leanc.backends.sanitizer.plan import SANITIZER_EXIT_CODES
from leanc.backends.sanitizer.runtime_reports import (
    TOOL_TITLES,
    SanitizerFinding,
    runtime_internal_error_line,
)
from leanc.backends.termination import signal_name
from leanc.foundation.models import (
    CounterexampleInput,
    Diagnostic,
    FailureKind,
    FailureReport,
)
from leanc.foundation.runner import ProcResult
from leanc.rules.findings import unique_id
from leanc.specs.model import TestCase

ORACLE_TOOL = "test-oracle"
ERROR_EXCERPT_MAX_CHARS = 300
DIFF_VALUE_MAX_CHARS = 120
BWRAP_FAILURE_EXIT_CODE = 1
BWRAP_FAILURE_MAX_LINES = 3
BWRAP_MESSAGE_PREFIX = "bwrap: "

OUTPUT_LIMIT_EXPLANATION = (
    "The program printed more than tests.max_output_bytes (e.g. a runaway loop); its exit status "
    "and output were not checked."
)
TIMEOUT_EXPLANATION = "Non-termination (or extreme slowness) on this input; no verdict about its output."
EXIT_CODE_EXPLANATION = (
    "The trusted test driver signals a failed check through its exit status; see the "
    "case's stderr for the failing check."
)
STDOUT_EXPLANATION = "The program's standard output must equal tests.cases[].expect_stdout exactly."
CPU_LIMIT_REASON = "CPU time limit"
WALL_CLOCK_REASON = "wall-clock timeout"


class CaseStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True)
class ExecutedCase:
    case: TestCase
    proc: ProcResult
    signal: int | None
    cpu_limited: bool
    timed_out: bool
    findings: list[SanitizerFinding]
    inputs: list[CounterexampleInput]
    raw_ref: str
    timeout_s: float
    max_output_bytes: int


@dataclass
class CaseVerdict:
    status: CaseStatus = CaseStatus.PASS
    diagnostics: list[Diagnostic] = field(default_factory=list)
    failures: list[FailureReport] = field(default_factory=list)
    error: str | None = None
    finding_summaries: list[dict] = field(default_factory=list)
    timeout_reason: str | None = None
    stdout_matches: bool | None = None


def first_difference(expected: str, got: str) -> str:
    for number, (expected_line, got_line) in enumerate(zip_longest(expected.splitlines(), got.splitlines()), 1):
        if expected_line != got_line:
            return (
                f"first difference at line {number}: expected {expected_line!r:.{DIFF_VALUE_MAX_CHARS}}, "
                f"got {got_line!r:.{DIFF_VALUE_MAX_CHARS}}"
            )
    return f"outputs differ only in line endings / trailing newline (expected {len(expected)} chars, got {len(got)})"


def _sandbox_failed_to_start(proc: ProcResult) -> bool:
    if proc.sandbox == "none" or proc.exit_code != BWRAP_FAILURE_EXIT_CODE or proc.stdout:
        return False
    lines = [line for line in proc.stderr.splitlines() if line.strip()]
    return 0 < len(lines) <= BWRAP_FAILURE_MAX_LINES and lines[0].startswith(BWRAP_MESSAGE_PREFIX)


def _tool_problem(run: ExecutedCase) -> str | None:
    name, proc = run.case.name, run.proc
    if proc.error:
        return f"{name}: could not run the test binary: {proc.error}"
    if run.findings:
        return None
    if _sandbox_failed_to_start(proc):
        return f"{name}: sandbox failed to start the test: {proc.stderr.strip()[:ERROR_EXCERPT_MAX_CHARS]}"
    internal_error = runtime_internal_error_line(proc.stderr)
    if internal_error is not None:
        return f"{name}: sanitizer runtime failure: {internal_error.strip()[:ERROR_EXCERPT_MAX_CHARS]}"
    return None


def _exit_status_and_stdout_are_evidence(run: ExecutedCase) -> bool:
    return not run.findings


class _CaseJudge:
    def __init__(self, run: ExecutedCase, seen_ids: set[str]):
        self.run = run
        self.seen_ids = seen_ids
        self.verdict = CaseVerdict()
        self.oracle_failed = False

    def record_findings(self) -> None:
        case = self.run.case
        argv_text = shlex.join(case.args)
        for finding in self.run.findings:
            d = finding.diagnostic
            d.message = f"{d.message} [test case {case.name!r}]"
            self.verdict.diagnostics.append(d)
            where = f" at {d.location.short()}" if d.location else ""
            self.verdict.finding_summaries.append(
                {"tool": d.tool, "rule_id": d.rule_id, "location": d.location.short() if d.location else None}
            )
            self.verdict.failures.append(
                FailureReport(
                    id=unique_id(f"sanitizer:{case.name}:{d.tool}:{d.rule_id}", self.seen_ids),
                    kind="runtime",
                    tool=d.tool,
                    property_id=d.rule_id,
                    description=d.message,
                    location=d.location,
                    inputs=list(self.run.inputs),
                    call_stack=finding.call_stack(),
                    explanation=(
                        f"{TOOL_TITLES.get(d.tool, d.tool)} reported {d.rule_id}{where} while executing trusted test "
                        f"case {case.name!r} (argv: {argv_text or '<none>'}). A sanitizer report is definite "
                        "evidence of memory-unsafe or undefined behaviour on this executed path."
                    ),
                    raw_ref=self.run.raw_ref,
                )
            )

    def oracle(self, rule: str, message: str, explanation: str, kind: FailureKind = "test-oracle") -> None:
        text = f"test case {self.run.case.name!r}: {message}"
        self.verdict.diagnostics.append(Diagnostic(tool=ORACLE_TOOL, rule_id=rule, severity="error", message=text))
        self.verdict.failures.append(
            FailureReport(
                id=unique_id(f"sanitizer:{self.run.case.name}:{ORACLE_TOOL}:{rule}", self.seen_ids),
                kind=kind,
                tool=ORACLE_TOOL,
                property_id=rule,
                description=text,
                inputs=list(self.run.inputs),
                explanation=explanation,
                raw_ref=self.run.raw_ref,
            )
        )

    def check_output_limit(self) -> None:
        self.oracle_failed = True
        self.oracle(
            "output-limit-exceeded",
            f"output exceeded the limit of {self.run.max_output_bytes} bytes per stream; the process was killed",
            OUTPUT_LIMIT_EXPLANATION,
        )

    def check_timeout(self) -> None:
        run = self.run
        reason = CPU_LIMIT_REASON if run.cpu_limited and not run.proc.timed_out else WALL_CLOCK_REASON
        self.verdict.timeout_reason = reason
        if not run.findings:
            self.oracle(
                "timeout",
                f"did not finish within {run.timeout_s:g}s ({reason}); the process group was killed",
                TIMEOUT_EXPLANATION,
                kind="timeout",
            )

    def check_exit_status(self) -> None:
        case, proc = self.run.case, self.run.proc
        if proc.exit_code == case.expect_exit:
            return
        self.oracle_failed = True
        got = f"exit code {proc.exit_code}" if proc.exit_code is not None else "no exit code"
        self.oracle("exit-code", f"{got}{self._exit_status_hint()}, expected {case.expect_exit}", EXIT_CODE_EXPLANATION)

    def _exit_status_hint(self) -> str:
        proc = self.run.proc
        if self.run.signal is not None:
            return f" (terminated by {signal_name(self.run.signal)})"
        if proc.exit_code in SANITIZER_EXIT_CODES:
            return (
                f" (this is the exit code Lean C configures for {SANITIZER_EXIT_CODES[proc.exit_code]}, "
                "but no sanitizer report was found on stderr)"
            )
        return ""

    def check_stdout(self) -> None:
        expected = self.run.case.expect_stdout
        if expected is None:
            return
        matches = self.run.proc.stdout == expected
        self.verdict.stdout_matches = matches
        if not matches:
            self.oracle_failed = True
            self.oracle(
                "stdout",
                f"stdout differs from the expected output ({first_difference(expected, self.run.proc.stdout)})",
                STDOUT_EXPLANATION,
            )

    def final_status(self) -> CaseStatus:
        if self.run.findings or self.oracle_failed:
            return CaseStatus.FAIL
        if self.run.timed_out:
            return CaseStatus.TIMEOUT
        return CaseStatus.PASS


def judge_case(run: ExecutedCase, seen_ids: set[str]) -> CaseVerdict:
    judge = _CaseJudge(run, seen_ids)
    problem = _tool_problem(run)
    judge.record_findings()
    verdict = judge.verdict
    if problem is not None:
        verdict.status = CaseStatus.FAIL if run.findings else CaseStatus.ERROR
        verdict.error = problem
        return verdict
    if run.proc.output_truncated:
        judge.check_output_limit()
    elif run.timed_out:
        judge.check_timeout()
    elif _exit_status_and_stdout_are_evidence(run):
        judge.check_exit_status()
        judge.check_stdout()
    verdict.status = judge.final_status()
    return verdict
