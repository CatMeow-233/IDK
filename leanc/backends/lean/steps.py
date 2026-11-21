from __future__ import annotations

import os
import re
import time
from pathlib import Path

from leanc.foundation.models import ToolInvocation
from leanc.foundation.redact import redact
from leanc.foundation.runner import DEFAULT_PATH, Limits, ProcResult
from leanc.foundation.sandbox import SandboxMounts
from leanc.foundation.status import Outcome
from leanc.toolchain.context import BackendContext
from leanc.toolchain.lean import LeanToolchain

STAGE = "lean"
LEAN_ADDRESS_SPACE_MB = 16384
LEAN_HEAP_MB = 8192
MAX_LEAN_OUTPUT_BYTES = 4 * 1024 * 1024
LEAN_MAX_FILE_MB = 256
MAX_EXCERPT_CHARS = 400
ELLIPSIS = "..."
STDERR_SEPARATOR = "\n--- stderr ---\n"
MAX_STEP_NAME_CHARS = 60
MAX_UNEXPECTED_LISTED = 10

_UNSAFE_FILE_NAME_CHARS_RE = re.compile(r"[^A-Za-z0-9_.-]")


class GateAbort(Exception):
    def __init__(self, outcome: Outcome, summary: str):
        super().__init__(summary)
        self.outcome = outcome
        self.summary = summary


def run_relative(ctx: BackendContext, path: Path) -> str:
    return os.path.relpath(os.path.realpath(path), os.path.realpath(ctx.run_dir))


def excerpt(text: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    redacted = redact(text.strip())
    return redacted if len(redacted) <= limit else redacted[: limit - len(ELLIPSIS)] + ELLIPSIS




def module_olean_path(module: str) -> str:
    return module.replace(".", "/") + ".olean"


def check_outputs(out_dir: Path, expected: set[str], what: str) -> None:
    found = {path.relative_to(out_dir).as_posix() for path in out_dir.rglob("*") if not path.is_dir()}
    unexpected = sorted(found - expected)
    if unexpected:
        raise GateAbort(
            Outcome.ERROR,
            f"unexpected files were written while {what}: {unexpected[:MAX_UNEXPECTED_LISTED]} "
            "(module system / planted outputs are not accepted; output discarded)",
        )


def _raw_file_name(index: int, what: str) -> str:
    return f"{index:02d}-{_UNSAFE_FILE_NAME_CHARS_RE.sub('_', what)[:MAX_STEP_NAME_CHARS]}.txt"


class StepRunner:
    def __init__(
        self,
        ctx: BackendContext,
        tc: LeanToolchain,
        deadline: float,
        raw_root: Path,
        invocations: list[ToolInvocation],
        steps: list[dict],
    ):
        self.ctx, self.tc, self.deadline, self.raw_root = ctx, tc, deadline, raw_root
        self.invocations, self.steps = invocations, steps

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def _budget_text(self) -> str:
        return f"{self.ctx.timeout(self.ctx.spec.lean.timeout_s):g}s"

    def _environment(self, lean_path: list[Path]) -> dict[str, str]:
        return {
            "LEAN_PATH": ":".join([self.tc.lib_dir, *(str(path) for path in lean_path)]),
            "PATH": f"{os.path.join(self.tc.prefix, 'bin')}:{DEFAULT_PATH}",
        }

    def _record(self, what: str, argv: list[str], cwd: Path, proc: ProcResult) -> Path:
        ctx = self.ctx
        raw = self.raw_root / _raw_file_name(len(self.steps), what)
        raw.write_text(
            redact(proc.stdout + (STDERR_SEPARATOR + proc.stderr if proc.stderr else "")), encoding="utf-8"
        )
        self.steps.append(
            {
                "step": what,
                "argv": list(argv),
                "cwd": run_relative(ctx, cwd),
                "exit_code": proc.exit_code,
                "timed_out": proc.timed_out,
                "duration_s": round(proc.duration_s, 3),
                "raw": run_relative(ctx, raw),
            }
        )
        return raw

    def run(
        self,
        what: str,
        argv: list[str],
        *,
        cwd: Path,
        lean_path: list[Path],
        ro: list[Path],
        rw: list[Path],
        tool: str = "lean",
        max_output: int = MAX_LEAN_OUTPUT_BYTES,
    ) -> tuple[ProcResult, Path]:
        left = self.remaining()
        if left <= 0:
            raise GateAbort(Outcome.TIMEOUT, f"Lean budget of {self._budget_text()} exhausted before {what}")
        mounts = SandboxMounts(ro=[self.tc.prefix, *(str(path) for path in ro)], rw=[str(path) for path in rw])
        limits = Limits(
            timeout_s=left, memory_mb=LEAN_ADDRESS_SPACE_MB, max_output_bytes=max_output, max_file_mb=LEAN_MAX_FILE_MB
        )
        proc = self.ctx.run(argv, limits=limits, cwd=cwd, env_extra=self._environment(lean_path), mounts=mounts)
        self.invocations.append(proc.to_invocation(tool, self.ctx.run_dir, self.ctx.log_name(STAGE, what)))
        raw = self._record(what, argv, cwd, proc)
        if proc.error:
            raise GateAbort(Outcome.ERROR, f"{tool} could not be started for {what}: {proc.error}")
        if proc.timed_out:
            raise GateAbort(Outcome.TIMEOUT, f"{what} timed out (Lean budget {self._budget_text()})")
        return proc, raw
