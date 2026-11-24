from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from leanc.backends.cbmc.argv import CHECK_FLAGS
from leanc.backends.cbmc.execution import CbmcRun, ToolProblem, tool_problem
from leanc.backends.cbmc.output import PROPERTY_VIOLATED, interpret_output, program_version
from leanc.backends.inductive.problems import BuildProblem, timed_out, tool_error
from leanc.backends.inductive.program import BuildMode
from leanc.backends.inductive.steps import ToolSteps
from leanc.foundation.models import Diagnostic
from leanc.foundation.status import Outcome
from leanc.rules.loop_sites import MatchedLoop
from leanc.specs.model import MANDATORY_CHECKS, Spec
from leanc.toolchain.cprover import CproverTool

INDUCTIVE_CHECKS: tuple[str, ...] = MANDATORY_CHECKS
NO_LIBRARY_FLAG = "--no-library"
UNWIND_FLAG = "--unwind"
UNWINDING_ASSERTIONS_FLAG = "--unwinding-assertions"
NO_SELF_LOOPS_FLAG = "--no-self-loops-to-assumptions"
OBJECT_BITS_FLAG = "--object-bits"
JSON_UI_FLAG = "--json-ui"
ASSIGNS_UNWIND_MARGIN = 2
RUN_LABEL_FORMAT = "{mode} cbmc"
RAW_NAME_FORMAT = "inductive-{mode}.json"
RUN_MODE_FORMAT = "inductive {mode}"
EXPECTED_RESULTS = "result"
EXIT_ALL_HOLD = 0
EXIT_SOME_VIOLATED = 10
MAX_LISTED_ERRORS = 3
UNTEXTED_ERROR = "an ERROR message without text"


@dataclass
class CheckedRun:
    run: CbmcRun
    results: list[dict] = field(default_factory=list)
    problem: BuildProblem | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def version(self) -> str | None:
        return program_version(self.run.output.program)


def inductive_unwind(spec: Spec, contracts: Sequence[MatchedLoop]) -> int:
    widest = max((len(item.contract.assigns) for item in contracts), default=0)
    unwind = spec.verification.unwind if spec.verification else 0
    return max(unwind, widest + ASSIGNS_UNWIND_MARGIN)


def check_flags() -> list[str]:
    return [CHECK_FLAGS[check] for check in INDUCTIVE_CHECKS]


def cbmc_argv(steps: ToolSteps, mode: BuildMode, program: Path, unwind: int) -> list[str]:
    cbmc = steps.ctx.tools.cbmc
    if not cbmc:
        raise ValueError("cbmc is not available (ctx.tools.cbmc is None)")
    verification = steps.ctx.spec.verification
    object_bits = verification.object_bits if verification else None
    return [
        cbmc,
        str(program),
        NO_LIBRARY_FLAG,
        *(check_flags() if mode is BuildMode.VERIFY else []),
        UNWIND_FLAG,
        str(unwind),
        UNWINDING_ASSERTIONS_FLAG,
        *([NO_SELF_LOOPS_FLAG] if mode is BuildMode.VERIFY else []),
        *([OBJECT_BITS_FLAG, str(object_bits)] if object_bits else []),
        JSON_UI_FLAG,
    ]


def run_label(mode: BuildMode) -> str:
    return RUN_LABEL_FORMAT.format(mode=mode.value)


def execute(steps: ToolSteps, mode: BuildMode, argv: list[str]) -> CbmcRun:
    ctx = steps.ctx
    step = steps.run(CproverTool.CBMC.value, run_label(mode), argv)
    raw_path = ctx.raw_dir / RAW_NAME_FORMAT.format(mode=mode.value)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(step.proc.stdout, encoding="utf-8")
    return CbmcRun(
        mode=RUN_MODE_FORMAT.format(mode=mode.value),
        argv=list(argv),
        proc=step.proc,
        raw_path=raw_path,
        raw_ref=ctx.relrun(raw_path),
        output=interpret_output(step.proc.stdout),
        invocation=steps.invocations[-1],
    )


def _as_build_problem(problem: ToolProblem) -> BuildProblem:
    if problem.outcome is Outcome.TIMEOUT:
        return timed_out(problem.summary)
    return tool_error(problem.summary)


def _exit_problem(run: CbmcRun, results: list[dict]) -> BuildProblem | None:
    violated = any(str(result.get("status", "")).upper() == PROPERTY_VIOLATED for result in results)
    expected = EXIT_SOME_VIOLATED if violated else EXIT_ALL_HOLD
    if run.proc.exit_code == expected:
        return None
    return tool_error(
        f"CBMC ({run.mode} run) exited with code {run.proc.exit_code}, but its results call for exit code {expected}"
    )


def checked_run(steps: ToolSteps, mode: BuildMode, program: Path, unwind: int) -> CheckedRun:
    run = execute(steps, mode, cbmc_argv(steps, mode, program, unwind))
    checked = CheckedRun(run, list(run.output.results or []))
    problem = tool_problem(run, steps.limits, steps.ctx, expect=EXPECTED_RESULTS)
    if problem is not None:
        checked.problem = _as_build_problem(problem)
        checked.diagnostics = list(problem.diagnostics)
        return checked
    if run.output.errors:
        errors = run.output.error_texts() or [UNTEXTED_ERROR]
        checked.problem = tool_error(f"CBMC ({run.mode} run) reported errors", errors[:MAX_LISTED_ERRORS])
        return checked
    checked.problem = _exit_problem(run, checked.results)
    return checked
