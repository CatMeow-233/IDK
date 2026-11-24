from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from leanc.backends.cbmc.execution import cbmc_limits
from leanc.backends.inductive.problems import STAGE, BuildProblem, timed_out, tool_error
from leanc.backends.termination import signal_name, trusted_tool_terminating_signal
from leanc.foundation.models import ToolInvocation
from leanc.foundation.runner import Limits, ProcResult
from leanc.foundation.sandbox import SandboxMounts
from leanc.toolchain.context import BackendContext, leanc_runtime_dir

MAX_EXCERPT_LINES = 3
MAX_EXCERPT_LINE_CHARS = 300


@dataclass(frozen=True)
class StepRun:
    label: str
    proc: ProcResult


def output_excerpt(proc: ProcResult, limit: int = MAX_EXCERPT_LINES) -> str:
    lines = [line.strip()[:MAX_EXCERPT_LINE_CHARS] for line in (proc.stderr or proc.stdout).splitlines() if line.strip()]
    return "; ".join(lines[-limit:])


def _with_excerpt(summary: str, proc: ProcResult) -> str:
    excerpt = output_excerpt(proc)
    return f"{summary}: {excerpt}" if excerpt else summary


@dataclass
class ToolSteps:
    ctx: BackendContext
    limits: Limits
    mounts: SandboxMounts
    cwd: Path
    invocations: list[ToolInvocation] = field(default_factory=list)
    argv: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def start(cls, ctx: BackendContext) -> ToolSteps:
        runtime = os.path.realpath(leanc_runtime_dir())
        return cls(
            ctx=ctx,
            limits=cbmc_limits(ctx),
            mounts=ctx.mounts(extra_ro=[runtime]),
            cwd=Path(os.path.abspath(ctx.snapshot_dir)),
        )

    def run(self, tool: str, label: str, argv: list[str], env_extra: dict[str, str] | None = None) -> StepRun:
        proc = self.ctx.run(argv, limits=self.limits, cwd=self.cwd, mounts=self.mounts, env_extra=env_extra)
        self.argv[label] = list(argv)
        self.invocations.append(proc.to_invocation(tool, self.ctx.run_dir, self.ctx.log_name(STAGE, label)))
        return StepRun(label, proc)

    def run_problem(self, run: StepRun) -> BuildProblem | None:
        proc = run.proc
        what = f"inductive step '{run.label}'"
        if proc.error:
            return tool_error(f"{what} could not be started: {proc.error}")
        if proc.timed_out:
            verification = self.ctx.spec.verification
            timeout_s = verification.timeout_s if verification else self.limits.timeout_s
            return timed_out(
                f"{what} timed out after {self.limits.timeout_s:g}s (verification.timeout_s={timeout_s:g}, "
                f"timeout scale {self.ctx.options.timeout_scale:g}); no verdict"
            )
        if proc.output_truncated:
            return tool_error(f"{what} output exceeded {self.limits.max_output_bytes} bytes and was truncated")
        signal_number = trusted_tool_terminating_signal(proc)
        if signal_number is not None:
            return tool_error(_with_excerpt(f"{what} was killed by {signal_name(signal_number)}", proc))
        return None

    def exit_problem(self, run: StepRun) -> BuildProblem | None:
        problem = self.run_problem(run)
        if problem is not None or run.proc.exit_code == 0:
            return problem
        return tool_error(_with_excerpt(f"inductive step '{run.label}' failed with exit code {run.proc.exit_code}", run.proc))
