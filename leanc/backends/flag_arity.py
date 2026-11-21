from __future__ import annotations

import re
from dataclasses import dataclass, field

from leanc.foundation.models import ToolInvocation
from leanc.foundation.redact import redacted_excerpt
from leanc.foundation.runner import Limits, ProcResult
from leanc.toolchain.context import BackendContext

DRIVER_ONLY_FLAG = "-###"
FLAG_ARITY_STEP = "flag-arity"
SEPARATE_VALUE_MESSAGE = (
    "takes the next entry as its value in clang, while the verifier translation and the stack checks read every "
    "entry as an option of its own; join the value to the option in one entry (-I<dir>, -D<name>, -std=<standard>)"
)
_MISSING_VALUE_RE = re.compile(r"error: argument to '[^'\n]+' is missing \(expected \d+ values?\)")


def flag_arity_argv(compiler: str, flag: str) -> list[str]:
    return [compiler, DRIVER_ONLY_FLAG, flag]


@dataclass
class FlagArityOutcome:
    separate_value: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    timeouts: list[str] = field(default_factory=list)
    invocations: list[ToolInvocation] = field(default_factory=list)

    def details(self, compiler: str) -> dict:
        return {
            "checked": not self.problems and not self.timeouts,
            "argv": flag_arity_argv(compiler, "<entry>"),
            "separate_value": self.separate_value,
        }

    def judge(self, flag: str, proc: ProcResult) -> None:
        if proc.timed_out:
            self.timeouts.append(f"{FLAG_ARITY_STEP} probe of compile.flags entry {flag!r} timed out")
        elif proc.error or proc.exit_code is None or proc.output_truncated:
            reason = proc.error or ("output truncated" if proc.output_truncated else f"signal {proc.signal}")
            self.problems.append(f"{FLAG_ARITY_STEP} probe of compile.flags entry {flag!r} failed ({reason})")
        elif _MISSING_VALUE_RE.search(proc.stderr):
            self.separate_value.append(flag)
            self.problems.append(
                f"compile.flags entry {flag!r} {SEPARATE_VALUE_MESSAGE}: {redacted_excerpt(proc.stderr)}"
            )


def probe_flag_arity(
    ctx: BackendContext, compiler: str, compiler_name: str, limits: Limits, stage: str
) -> FlagArityOutcome:
    outcome = FlagArityOutcome()
    for position, flag in enumerate(ctx.spec.compile.flags):
        proc = ctx.run(flag_arity_argv(compiler, flag), limits=limits, cwd=ctx.snapshot_dir)
        log_name = ctx.log_name(stage, f"{FLAG_ARITY_STEP}-{position}")
        outcome.invocations.append(proc.to_invocation(compiler_name, ctx.run_dir, log_name))
        outcome.judge(flag, proc)
    return outcome
