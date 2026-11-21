from __future__ import annotations

import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NamedTuple

from leanc.backends.cbmc.argv import Mode, build_cbmc_argv
from leanc.backends.cbmc.output import CbmcOutput, interpret_output
from leanc.backends.counterexample.locations import location_from_cbmc
from leanc.backends.termination import terminating_signal
from leanc.foundation.models import Diagnostic, ToolInvocation
from leanc.foundation.runner import Limits, ProcResult
from leanc.foundation.status import Outcome
from leanc.specs.model import VerificationConfig
from leanc.toolchain.context import BackendContext

STAGE = "cbmc"
TOOL = "cbmc"
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_LISTED_ITEMS = 3
OUT_OF_MEMORY_MARKERS = ("out of memory", "bad_alloc", "cannot allocate memory", "memory exhausted")
SIGNALS_HINTING_OUT_OF_MEMORY = (signal.SIGKILL, signal.SIGABRT, signal.SIGSEGV)

ExpectedSection = Literal["result", "goals"]


class FrontendFailure(NamedTuple):
    marker: str
    kind: str
    rule: str


FRONTEND_FAILURES = (
    FrontendFailure("PARSING ERROR", "could not parse the program (PARSING ERROR)", "parsing-error"),
    FrontendFailure(
        "CONVERSION ERROR", "could not type-check / convert the program (CONVERSION ERROR)", "conversion-error"
    ),
)
FRONTEND_ERROR_MARKERS = tuple(failure.marker for failure in FRONTEND_FAILURES)

_SIGNAL_NAMES = {member.value: member.name for member in signal.Signals}


@dataclass
class CbmcRun:
    mode: str
    argv: list[str]
    proc: ProcResult
    raw_path: Path
    raw_ref: str
    output: CbmcOutput
    invocation: ToolInvocation


@dataclass
class ToolProblem:
    outcome: Outcome
    summary: str
    diagnostics: list[Diagnostic] = field(default_factory=list)


def cbmc_limits(ctx: BackendContext) -> Limits:
    verification = ctx.spec.verification
    return Limits(
        timeout_s=ctx.timeout(verification.timeout_s),
        memory_mb=verification.memory_mb,
        max_output_bytes=MAX_OUTPUT_BYTES,
    )


def _absolute_dir_for_sandbox_chdir(ctx: BackendContext) -> Path:
    return Path(os.path.abspath(ctx.snapshot_dir))


def execute_cbmc(ctx: BackendContext, mode: Mode, limits: Limits) -> CbmcRun:
    argv = build_cbmc_argv(ctx, mode=mode)
    proc = ctx.run(argv, limits=limits, cwd=_absolute_dir_for_sandbox_chdir(ctx))
    raw_path = ctx.raw_dir / f"cbmc-{mode}.json"
    raw_path.write_text(proc.stdout, encoding="utf-8")
    note = f"output truncated at {limits.max_output_bytes} bytes" if proc.output_truncated else None
    invocation = proc.to_invocation(TOOL, ctx.run_dir, ctx.log_name(STAGE, mode), note=note)
    return CbmcRun(
        mode=mode,
        argv=list(argv),
        proc=proc,
        raw_path=raw_path,
        raw_ref=ctx.relrun(raw_path),
        output=interpret_output(proc.stdout),
        invocation=invocation,
    )


def _signal_name(number: int) -> str:
    return _SIGNAL_NAMES.get(number, f"signal {number}")


def _meaningful_errors(output: CbmcOutput) -> list[tuple[str, dict]]:
    errors = []
    for message in output.errors:
        text = str(message.get("messageText", "")).strip()
        if text and text not in FRONTEND_ERROR_MARKERS:
            errors.append((text, message))
    return errors


def error_diagnostics(output: CbmcOutput, rule: str) -> list[Diagnostic]:
    return [
        Diagnostic(
            tool=TOOL,
            rule_id=rule,
            severity="error",
            message=text,
            location=location_from_cbmc(message.get("sourceLocation")),
        )
        for text, message in _meaningful_errors(output)
    ]


def messages_excerpt(output: CbmcOutput, stderr: str, limit: int = MAX_LISTED_ITEMS) -> str:
    items = []
    for text, message in _meaningful_errors(output):
        location = location_from_cbmc(message.get("sourceLocation"))
        items.append(f"{location.short()}: {text}" if location and location.file else text)
    if not items and stderr.strip():
        items = [line.strip() for line in stderr.strip().splitlines()[-limit:] if line.strip()]
    more = f" (+{len(items) - limit} more)" if len(items) > limit else ""
    return "; ".join(items[:limit]) + more


def ran_out_of_memory(run: CbmcRun) -> bool:
    texts = " ".join([*run.output.error_texts(), run.proc.stderr]).lower()
    return any(marker in texts for marker in OUT_OF_MEMORY_MARKERS)




def _killed_problem(
    what: str, signal_number: int, out_of_memory: bool, verification: VerificationConfig, output: CbmcOutput
) -> ToolProblem:
    hint = ""
    if out_of_memory or signal_number in SIGNALS_HINTING_OUT_OF_MEMORY:
        hint = f" (possibly out of memory: verification.memory_mb={verification.memory_mb})"
    return ToolProblem(
        Outcome.ERROR, f"{what} was killed by {_signal_name(signal_number)}{hint}", error_diagnostics(output, "cbmc-error")
    )


def _verdicts_unusable(output: CbmcOutput, section: list[dict] | None) -> bool:
    return bool(output.parse_error) or section is None


def _frontend_failure(output: CbmcOutput) -> FrontendFailure | None:
    texts = " ".join(output.error_texts())
    return next((failure for failure in FRONTEND_FAILURES if failure.marker in texts), None)


def _with_excerpt(summary: str, excerpt: str) -> str:
    return summary + (f": {excerpt}" if excerpt else "")


def _missing_section_problem(what: str, run: CbmcRun, expect: ExpectedSection) -> ToolProblem:
    frontend = _frontend_failure(run.output)
    if frontend is not None:
        kind, rule = frontend.kind, frontend.rule
    else:
        kind, rule = f"produced no {expect} (exit code {run.proc.exit_code})", "cbmc-error"
    return ToolProblem(
        Outcome.ERROR,
        _with_excerpt(f"{what} {kind}", messages_excerpt(run.output, run.proc.stderr)),
        error_diagnostics(run.output, rule),
    )


def tool_problem(run: CbmcRun, limits: Limits, ctx: BackendContext, *, expect: ExpectedSection) -> ToolProblem | None:
    proc, output = run.proc, run.output
    verification = ctx.spec.verification
    what = f"CBMC ({run.mode} run)"
    if proc.error:
        return ToolProblem(Outcome.ERROR, f"{what} could not be started: {proc.error}")
    if proc.timed_out:
        return ToolProblem(
            Outcome.TIMEOUT,
            f"{what} timed out after {limits.timeout_s:g}s (verification.timeout_s={verification.timeout_s:g}, "
            f"timeout scale {ctx.options.timeout_scale:g}); no verdict",
        )
    if proc.output_truncated:
        return ToolProblem(
            Outcome.ERROR,
            f"{what} output exceeded {limits.max_output_bytes} bytes and was truncated; "
            "the result cannot be interpreted",
        )
    out_of_memory = ran_out_of_memory(run)
    signal_number = terminating_signal(proc)
    if signal_number is not None:
        return _killed_problem(what, signal_number, out_of_memory, verification, output)
    section = output.results if expect == "result" else output.goals
    if out_of_memory and _verdicts_unusable(output, section):
        return ToolProblem(
            Outcome.ERROR,
            f"{what} ran out of memory (verification.memory_mb={verification.memory_mb}): "
            f"{messages_excerpt(output, proc.stderr)}",
            error_diagnostics(output, "out-of-memory"),
        )
    if output.parse_error:
        return ToolProblem(
            Outcome.ERROR,
            _with_excerpt(
                f"{what} produced unparsable JSON output ({output.parse_error}; exit code {proc.exit_code})",
                messages_excerpt(output, proc.stderr),
            ),
            error_diagnostics(output, "cbmc-error"),
        )
    if section is None:
        return _missing_section_problem(what, run, expect)
    return None
