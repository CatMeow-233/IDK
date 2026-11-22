from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from leanc.backends.clang import define_args, sandbox_visible_path
from leanc.foundation.models import PolicyViolation, ToolInvocation
from leanc.foundation.redact import redacted_excerpt
from leanc.foundation.runner import Limits, ProcResult
from leanc.foundation.text import safe_name
from leanc.rules.stack_checks import (
    STACK_FRAME_CHECK_FLAGS,
    SYSTEM_HEADER_WARNINGS_FLAG,
    UNBOUNDED_STACK_CHECK_FLAGS,
    codegen_flags,
    default_system_dirs,
    stack_usage_findings,
    stack_usage_path,
    unbounded_stack_scan,
)
from leanc.specs.model import Spec
from leanc.toolchain.context import BackendContext

STACK_USAGE_DIR = "stack-usage"
ASSEMBLY_SUFFIX = ".s"
SYNTAX_OUTPUT_SUFFIX = ".syntax"
INCLUDE_FLAG = "-include"
OUTPUT_FLAG = "-o"
ANCHOR_HEADER = "leanc_stack_usage_anchor.h"
ANCHOR_SOURCE = (
    "#pragma clang diagnostic push\n"
    '#pragma clang diagnostic ignored "-Weverything"\n'
    "void __leanc_stack_usage_anchor(void) {}\n"
    "#pragma clang diagnostic pop\n"
)
CLEAN_EXIT_CODE = 0
STDLIB_SELECTION_PREFIX = "-stdlib="
SEARCH_LIST_PROBE_FLAGS = ("-E", "-v")
LANGUAGE_FLAG = "-x"
PROBE_LANGUAGES = {"c": "c", "cpp": "c++"}
SYSTEM_DIRS_STEP = "system-dirs"
UNREADABLE_SEARCH_LIST = "printed no readable system include search list"
MAX_UNPARSED_ROWS_SHOWN = 3
NO_STACK_USAGE = "produced no stack usage (compile.flags stop machine-code generation, e.g. -emit-llvm)"


@dataclass(frozen=True)
class UnboundedStackScope:
    name: str
    extra_flags: tuple[str, ...]
    tolerates_system_headers: bool

    @property
    def log_label(self) -> str:
        return self.name.replace("_", "-")


ALL_REPORTED_FILES = UnboundedStackScope("stack", (), tolerates_system_headers=False)
PROJECT_FILES_AS_USER_CODE = UnboundedStackScope(
    "stack_system_headers", (SYSTEM_HEADER_WARNINGS_FLAG,), tolerates_system_headers=True
)
UNBOUNDED_STACK_SCOPES = (ALL_REPORTED_FILES, PROJECT_FILES_AS_USER_CODE)


def system_dirs_argv(compiler: str, spec: Spec) -> list[str]:
    return [
        compiler,
        *[flag for flag in spec.compile.flags if flag.startswith(STDLIB_SELECTION_PREFIX)],
        *SEARCH_LIST_PROBE_FLAGS,
        LANGUAGE_FLAG,
        PROBE_LANGUAGES[spec.language.name],
        os.devnull,
    ]


def _language_args(spec: Spec, include_args: list[str]) -> list[str]:
    return [*codegen_flags(spec.compile.flags), *define_args(spec.compile.defines), *include_args]


def _standard_arg(spec: Spec) -> str:
    return f"-std={spec.language.standard}"


def unbounded_stack_argv(
    compiler: str, spec: Spec, include_args: list[str], rel: str, assembly: Path, scope: UnboundedStackScope
) -> list[str]:
    syntax_output = sandbox_visible_path(assembly.with_suffix(SYNTAX_OUTPUT_SUFFIX))
    return [
        compiler,
        _standard_arg(spec),
        *_language_args(spec, include_args),
        *UNBOUNDED_STACK_CHECK_FLAGS,
        *scope.extra_flags,
        OUTPUT_FLAG,
        syntax_output,
        rel,
    ]


def stack_frame_argv(compiler: str, spec: Spec, include_args: list[str], rel: str, assembly: Path) -> list[str]:
    return [
        compiler,
        _standard_arg(spec),
        INCLUDE_FLAG,
        sandbox_visible_path(stack_usage_anchor_path(assembly)),
        *_language_args(spec, include_args),
        *STACK_FRAME_CHECK_FLAGS,
        OUTPUT_FLAG,
        sandbox_visible_path(assembly),
        rel,
    ]


def stack_assembly_path(build_dir: Path, rel: str) -> Path:
    return build_dir / STACK_USAGE_DIR / (safe_name(rel) + ASSEMBLY_SUFFIX)


def stack_usage_anchor_path(assembly: Path) -> Path:
    return assembly.parent / ANCHOR_HEADER


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
        stack_usage_anchor_path(assembly).write_text(ANCHOR_SOURCE, encoding="utf-8")
        for scope in UNBOUNDED_STACK_SCOPES:
            outcome.argv[scope.name] = unbounded_stack_argv(
                self.compiler, self.ctx.spec, include_args, rel, assembly, scope
            )
        outcome.argv["stack_frames"] = stack_frame_argv(self.compiler, self.ctx.spec, include_args, rel, assembly)
        outcome.argv[SYSTEM_DIRS_STEP] = system_dirs_argv(self.compiler, self.ctx.spec)
        system_dirs = self.probe_system_dirs(outcome, rel)
        checked = [self.check_unbounded(outcome, rel, scope, system_dirs) for scope in UNBOUNDED_STACK_SCOPES]
        checked.append(self.check_frames(outcome, rel, assembly))
        return outcome, all(checked)

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

    def probe_system_dirs(self, outcome: StackCheckOutcome, rel: str) -> tuple[str, ...] | None:
        proc = self.execute(outcome, outcome.argv[SYSTEM_DIRS_STEP], rel, SYSTEM_DIRS_STEP)
        if proc is None:
            return None
        dirs = default_system_dirs(proc.stderr) if proc.exit_code == CLEAN_EXIT_CODE else None
        if dirs is None:
            outcome.problems.append(
                f"{SYSTEM_DIRS_STEP} probe for {rel} {UNREADABLE_SEARCH_LIST} (exit {proc.exit_code}): "
                f"{redacted_excerpt(proc.stderr)}"
            )
        return dirs

    def check_unbounded(
        self, outcome: StackCheckOutcome, rel: str, scope: UnboundedStackScope, system_dirs: tuple[str, ...] | None
    ) -> bool:
        tolerated_dirs: tuple[str, ...] = ()
        if scope.tolerates_system_headers:
            if system_dirs is None:
                return False
            tolerated_dirs = system_dirs
        proc = self.execute(outcome, outcome.argv[scope.name], rel, scope.log_label)
        if proc is None:
            return False
        scan = unbounded_stack_scan(
            proc.stderr,
            self.unbounded_reason,
            project_root=sandbox_visible_path(self.ctx.snapshot_dir),
            tolerated_dirs=tolerated_dirs,
        )
        outcome.violations += scan.violations
        if proc.exit_code != CLEAN_EXIT_CODE and (proc.exit_code is None or not scan.accounts_for_failure):
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
        if proc.exit_code != CLEAN_EXIT_CODE:
            outcome.problems.append(
                f"stack-frame check of {rel} failed (exit {proc.exit_code}): {redacted_excerpt(proc.stderr)}"
            )
            return False
        if not usage_file.is_file():
            outcome.problems.append(f"stack-frame check of {rel} {NO_STACK_USAGE}: {redacted_excerpt(proc.stderr)}")
            return False
        usage = stack_usage_findings(rel, usage_file.read_text(encoding="utf-8", errors="replace"))
        outcome.violations += usage.violations
        if usage.unparsed:
            outcome.problems.append(
                f"stack-frame check of {rel}: unreadable stack usage rows {usage.unparsed[:MAX_UNPARSED_ROWS_SHOWN]}"
            )
            return False
        return True
