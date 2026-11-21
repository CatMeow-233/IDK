from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from leanc.backends.clang import define_args, sandbox_visible_path
from leanc.foundation.models import PolicyViolation, ToolInvocation
from leanc.foundation.redact import redacted_excerpt
from leanc.foundation.runner import Limits, ProcResult
from leanc.foundation.text import safe_name
from leanc.rules.stack_checks import (
    STACK_FRAME_CHECK_FLAGS,
    UNBOUNDED_STACK_CHECK_FLAGS,
    codegen_flags,
    stack_usage_findings,
    stack_usage_path,
    unbounded_stack_violations,
)
from leanc.specs.model import Spec
from leanc.toolchain.context import BackendContext

STACK_USAGE_DIR = "stack-usage"
ASSEMBLY_SUFFIX = ".s"
SUCCESS_EXIT_CODES = (0, None)
MAX_UNPARSED_ROWS_SHOWN = 3
NO_STACK_USAGE = "produced no stack usage (compile.flags stop machine-code generation, e.g. -emit-llvm)"


def _language_args(compiler: str, spec: Spec, include_args: list[str]) -> list[str]:
    return [
        compiler,
        f"-std={spec.language.standard}",
        *codegen_flags(spec.compile.flags),
        *define_args(spec.compile.defines),
        *include_args,
    ]


def unbounded_stack_argv(compiler: str, spec: Spec, include_args: list[str], rel: str) -> list[str]:
    return [*_language_args(compiler, spec, include_args), *UNBOUNDED_STACK_CHECK_FLAGS, rel]


def stack_frame_argv(compiler: str, spec: Spec, include_args: list[str], rel: str, assembly: Path) -> list[str]:
    return [
        *_language_args(compiler, spec, include_args),
        *STACK_FRAME_CHECK_FLAGS,
        "-o",
        sandbox_visible_path(assembly),
        rel,
    ]


def stack_assembly_path(build_dir: Path, rel: str) -> Path:
    return build_dir / STACK_USAGE_DIR / (safe_name(rel) + ASSEMBLY_SUFFIX)


@dataclass
class StackCheckOutcome:
    argv: dict[str, list[str]] = field(default_factory=dict)
    violations: list[PolicyViolation] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    timeouts: list[str] = field(default_factory=list)
    invocations: list[ToolInvocation] = field(default_factory=list)


@dataclass(frozen=True)
class StackCheck:
    ctx: BackendContext
    compiler: str
    compiler_name: str
    limits: Limits
    stage: str
    unbounded_reason: str

    def run(self, rel: str) -> tuple[StackCheckOutcome, bool]:
        outcome = StackCheckOutcome()
        include_args = self.ctx.include_args()
        assembly = stack_assembly_path(self.ctx.build_dir, rel)
        assembly.parent.mkdir(parents=True, exist_ok=True)
        outcome.argv["stack"] = unbounded_stack_argv(self.compiler, self.ctx.spec, include_args, rel)
        outcome.argv["stack_frames"] = stack_frame_argv(self.compiler, self.ctx.spec, include_args, rel, assembly)
        unbounded_checked = self.check_unbounded(outcome, rel)
        frames_checked = self.check_frames(outcome, rel, assembly)
        return outcome, unbounded_checked and frames_checked

    def execute(self, outcome: StackCheckOutcome, argv: list[str], rel: str, what: str) -> ProcResult | None:
        proc = self.ctx.run(argv, limits=self.limits, cwd=self.ctx.snapshot_dir)
        log_name = self.ctx.log_name(self.stage, f"{what}-{rel}")
        outcome.invocations.append(proc.to_invocation(self.compiler_name, self.ctx.run_dir, log_name))
        if proc.timed_out:
            outcome.timeouts.append(f"{what} check of {rel} timed out")
            return None
        if proc.error:
            outcome.problems.append(f"{what} check of {rel} failed: {proc.error}")
            return None
        return proc

    def check_unbounded(self, outcome: StackCheckOutcome, rel: str) -> bool:
        proc = self.execute(outcome, outcome.argv["stack"], rel, "stack")
        if proc is None:
            return False
        found = unbounded_stack_violations(rel, proc.stderr, reason=self.unbounded_reason)
        outcome.violations += found
        if proc.exit_code not in SUCCESS_EXIT_CODES and not found:
            outcome.problems.append(
                f"stack check of {rel} failed (exit {proc.exit_code}): {redacted_excerpt(proc.stderr)}"
            )
            return False
        return True

    def check_frames(self, outcome: StackCheckOutcome, rel: str, assembly: Path) -> bool:
        proc = self.execute(outcome, outcome.argv["stack_frames"], rel, "stack-frame")
        if proc is None:
            return False
        usage_file = stack_usage_path(assembly)
        if proc.exit_code not in SUCCESS_EXIT_CODES or not usage_file.is_file():
            outcome.problems.append(
                f"stack-frame check of {rel} {NO_STACK_USAGE} (exit {proc.exit_code}): {redacted_excerpt(proc.stderr)}"
            )
            return False
        usage = stack_usage_findings(rel, usage_file.read_text(encoding="utf-8", errors="replace"))
        outcome.violations += usage.violations
        if usage.unparsed:
            outcome.problems.append(
                f"stack-frame check of {rel}: unreadable stack usage rows {usage.unparsed[:MAX_UNPARSED_ROWS_SHOWN]}"
            )
            return False
        return True
