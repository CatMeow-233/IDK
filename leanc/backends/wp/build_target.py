from __future__ import annotations

import re
from dataclasses import dataclass

from leanc.foundation.models import ToolInvocation
from leanc.foundation.runner import Limits, ProcResult
from leanc.toolchain.context import BackendContext
from leanc.toolchain.cxx import compiler

DUMP_MACHINE_FLAG = "-dumpmachine"
BUILD_TARGET_STEP = "build-target"
BUILD_TARGET_TIMEOUT_S = 30.0
MAX_TRIPLE_OUTPUT_BYTES = 4096
COMPILER_TOOL = "clang"
NO_COMPILER = "no C compiler (clang) to ask for the build target"
NOT_QUERIED = "the build target was not queried"
_TARGET_TRIPLE_RE = re.compile(r"^(?P<architecture>[A-Za-z0-9_.]+)(?:-[A-Za-z0-9_.]+)+$")


@dataclass(frozen=True)
class BuildTarget:
    triple: str | None
    architecture: str | None
    problem: str | None = None

    def details(self) -> dict:
        return {"triple": self.triple, "architecture": self.architecture, "problem": self.problem}


UNQUERIED_BUILD_TARGET = BuildTarget(None, None, NOT_QUERIED)


def dump_machine_argv(compiler_path: str, flags: list[str]) -> list[str]:
    return [compiler_path, *flags, DUMP_MACHINE_FLAG]


def parse_build_target(output: str) -> BuildTarget:
    lines = output.strip().splitlines()
    match = _TARGET_TRIPLE_RE.match(lines[0].strip()) if len(lines) == 1 else None
    if match is None:
        return BuildTarget(None, None, f"{DUMP_MACHINE_FLAG} printed no target triple: {output.strip()[:200]!r}")
    return BuildTarget(match.group(0), match.group("architecture"))


def _judge(proc: ProcResult) -> BuildTarget:
    if proc.timed_out:
        return BuildTarget(None, None, f"{DUMP_MACHINE_FLAG} timed out")
    if not proc.ok or proc.output_truncated:
        reason = proc.error or ("output truncated" if proc.output_truncated else f"exit {proc.exit_code}")
        return BuildTarget(None, None, f"{DUMP_MACHINE_FLAG} failed ({reason})")
    return parse_build_target(proc.stdout)


def query_build_target(ctx: BackendContext, stage: str) -> tuple[BuildTarget, list[ToolInvocation]]:
    compiler_path = compiler(ctx)
    if not compiler_path:
        return BuildTarget(None, None, NO_COMPILER), []
    limits = Limits(timeout_s=ctx.timeout(BUILD_TARGET_TIMEOUT_S), max_output_bytes=MAX_TRIPLE_OUTPUT_BYTES)
    proc = ctx.run(dump_machine_argv(compiler_path, list(ctx.spec.compile.flags)), limits=limits, cwd=ctx.snapshot_dir)
    invocation = proc.to_invocation(COMPILER_TOOL, ctx.run_dir, ctx.log_name(stage, BUILD_TARGET_STEP))
    return _judge(proc), [invocation]
