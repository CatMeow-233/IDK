from __future__ import annotations

import math
import os
import re
from pathlib import Path

from leanc.specs.model import Spec
from leanc.toolchain.context import BackendContext
from leanc.toolchain.framac import FramaC

MACHDEP = "x86_64"
FRAMA_C_PREPROCESSOR_MACRO_ARGS: tuple[str, ...] = ("-D__FRAMAC__", "-std=c11")
ALWAYS_ON_RTE_FLAG = "-warn-invalid-pointer"
FATAL_KERNEL_WARNINGS = ("annot-error=abort", "typing:implicit-function-declaration=abort")
DESELECT_TERMINATION_GOALS = "-wp-prop=-@terminates,-@decreases"
MIN_PROVER_MEMORY_MB = 128
MAX_PROVER_MEMORY_MB = 1000
MIN_PROVER_PROCESSES = 2
MAX_PROVER_PROCESSES = 8

RTE_EXTRA_FLAGS: dict[str, list[str]] = {
    "unsigned-overflow": ["-warn-unsigned-overflow"],
    "conversion": ["-warn-signed-downcast", "-warn-unsigned-downcast"],
}
RTE_EXTRA_CHECK_DESCRIPTIONS: dict[str, str] = {
    "unsigned-overflow": "unsigned integer wrap-around (spec check unsigned-overflow)",
    "conversion": "out-of-range integer conversions (spec check conversion)",
}
RTE_CLASSES = (
    "invalid memory accesses (\\valid / \\valid_read)",
    "division / modulo by zero",
    "signed integer overflow",
    "invalid shift amounts and left shifts of negative values",
    "pointer arithmetic leaving its object (-warn-invalid-pointer)",
    "pointer downcasts",
    "float-to-int overflow and non-finite floating-point values",
    "trap representations of _Bool",
)

_SHELL_SAFE_INCLUDE_DIR_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./+-]*$")
_SHELL_SAFE_DEFINE_RE = re.compile(r"^(?:-D)?[A-Za-z_]\w*(?:=[A-Za-z0-9_.+-]*)?$")


def shell_unsafe_preprocessor_argument(ctx: BackendContext) -> str | None:
    spec = ctx.spec
    for directory in spec.sources.include_dirs:
        if not _SHELL_SAFE_INCLUDE_DIR_RE.match(directory):
            return f"include directory {directory!r} cannot be passed safely to Frama-C's preprocessor"
    for define in spec.compile.defines:
        if not _SHELL_SAFE_DEFINE_RE.match(define):
            return f"define {define!r} cannot be passed safely to Frama-C's preprocessor"
    return None


def preprocessor_args(ctx: BackendContext) -> list[str]:
    problem = shell_unsafe_preprocessor_argument(ctx)
    if problem is not None:
        raise ValueError(problem)
    spec = ctx.spec
    includes = [f"-I{directory}" for directory in spec.sources.include_dirs]
    return includes + preprocessor_define_args(spec.compile.defines)


def preprocessor_define_args(defines: list[str]) -> list[str]:
    return [define if define.startswith("-D") else f"-D{define}" for define in defines]


def frama_c_preprocessor_macro_args(spec: Spec) -> list[str]:
    return [*FRAMA_C_PREPROCESSOR_MACRO_ARGS, *preprocessor_define_args(spec.compile.defines)]


def rte_flags(ctx: BackendContext) -> tuple[list[str], list[str]]:
    flags = [ALWAYS_ON_RTE_FLAG]
    honoured: list[str] = []
    verification = ctx.spec.verification
    for check in verification.checks if verification is not None else []:
        for flag in RTE_EXTRA_FLAGS.get(check, []):
            if flag not in flags:
                flags.append(flag)
                if check not in honoured:
                    honoured.append(check)
    return flags, honoured


def prover_timeout(ctx: BackendContext) -> int:
    return max(1, int(math.ceil(ctx.timeout(ctx.spec.wp.prover_timeout_s))))


def prover_parallelism() -> int:
    return max(MIN_PROVER_PROCESSES, min(MAX_PROVER_PROCESSES, os.cpu_count() or MIN_PROVER_PROCESSES))


def prover_memory_limit_mb(gate_memory_mb: int) -> int:
    return max(MIN_PROVER_MEMORY_MB, min(MAX_PROVER_MEMORY_MB, gate_memory_mb // 2))


def build_wp_argv(
    ctx: BackendContext,
    fc: FramaC,
    *,
    report_json: Path,
    work_dir: Path,
    why3_conf: Path | None,
) -> list[str]:
    spec = ctx.spec
    wp = spec.wp
    cpp = preprocessor_args(ctx)
    flags, _ = rte_flags(ctx)
    argv: list[str] = [fc.frama_c, "-machdep", MACHDEP]
    for warning in FATAL_KERNEL_WARNINGS:
        argv += ["-kernel-warn-key", warning]
    if cpp:
        argv.append("-cpp-extra-args=" + " ".join(cpp))
    argv += flags
    argv += [
        "-wp",
        "-wp-rte",
        "-wp-smoke-tests",
        "-wp-prover", ",".join(wp.provers),
        "-wp-timeout", str(prover_timeout(ctx)),
        "-wp-memlimit", str(prover_memory_limit_mb(wp.memory_mb)),
        "-wp-par", str(prover_parallelism()),
        "-wp-cache", "none",
        "-wp-session", str(work_dir / "session"),
        "-wp-out", str(work_dir / "out"),
        "-wp-report-json", str(report_json),
    ]
    if why3_conf is not None:
        argv += ["-wp-why3-config", str(why3_conf)]
    if not wp.require_termination:
        argv.append(DESELECT_TERMINATION_GOALS)
    argv += list(spec.sources.editable)
    return argv
