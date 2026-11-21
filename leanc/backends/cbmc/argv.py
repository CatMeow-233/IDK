from __future__ import annotations

import os
from typing import Literal, NamedTuple

from leanc.specs.model import VerificationConfig
from leanc.toolchain.context import BackendContext

CHECK_FLAGS: dict[str, str] = {
    "bounds": "--bounds-check",
    "pointer": "--pointer-check",
    "div-by-zero": "--div-by-zero-check",
    "signed-overflow": "--signed-overflow-check",
    "unsigned-overflow": "--unsigned-overflow-check",
    "pointer-overflow": "--pointer-overflow-check",
    "conversion": "--conversion-check",
    "undefined-shift": "--undefined-shift-check",
    "float-overflow": "--float-overflow-check",
    "nan": "--nan-check",
    "memory-leak": "--memory-leak-check",
    "pointer-primitive": "--pointer-primitive-check",
    "enum-range": "--enum-range-check",
}

VACUITY_DEFINE = "-DLEANC_VACUITY_CHECK"
DEFINES_DISABLING_HARNESS_ASSERTIONS = frozenset({"NDEBUG"})
COVERAGE_CRITERION = ("--cover", "location")
MALLOC_MAY_FAIL_FLAGS = ("--malloc-may-fail", "--malloc-fail-null")

Mode = Literal["verify", "vacuity", "coverage"]
MODES: tuple[str, ...] = ("verify", "vacuity", "coverage")


class ForwardedDefines(NamedTuple):
    args: list[str]
    dropped: list[str]


def forwarded_defines(defines: list[str]) -> ForwardedDefines:
    args: list[str] = []
    dropped: list[str] = []
    for define in defines:
        arg = define if define.startswith("-D") else f"-D{define}"
        name = arg[2:].split("=", 1)[0].strip()
        if name in DEFINES_DISABLING_HARNESS_ASSERTIONS:
            dropped.append(define)
            continue
        args.append(arg)
    return ForwardedDefines(args, dropped)


def _property_check_flags(verification: VerificationConfig) -> list[str]:
    flags = []
    for check in verification.effective_checks():
        flag = CHECK_FLAGS.get(check)
        if flag is None:
            raise ValueError(f"no CBMC flag known for check {check!r}")
        flags.append(flag)
    return flags


def _mode_args(verification: VerificationConfig, mode: Mode) -> list[str]:
    if mode == "vacuity":
        return [VACUITY_DEFINE]
    if mode == "verify":
        return _property_check_flags(verification)
    return list(COVERAGE_CRITERION)


def _cbmc_accepts_unwinding_assertions(mode: Mode) -> bool:
    return mode != "coverage"


def _unwind_args(verification: VerificationConfig) -> list[str]:
    args = ["--unwind", str(verification.unwind)]
    if verification.unwindset:
        bounds = ",".join(f"{loop}:{bound}" for loop, bound in sorted(verification.unwindset.items()))
        args += ["--unwindset", bounds]
    return args


def _memory_model_args(verification: VerificationConfig) -> list[str]:
    args: list[str] = []
    if verification.malloc_may_fail:
        args += MALLOC_MAY_FAIL_FLAGS
    if verification.object_bits:
        args += ["--object-bits", str(verification.object_bits)]
    return args


def build_cbmc_argv(ctx: BackendContext, *, mode: Mode = "verify") -> list[str]:
    if mode not in MODES:
        raise ValueError(f"unknown CBMC mode {mode!r} (expected one of {', '.join(MODES)})")
    cbmc = ctx.tools.cbmc
    if not cbmc:
        raise ValueError("cbmc is not available (ctx.tools.cbmc is None)")
    spec = ctx.spec
    verification = spec.verification
    argv: list[str] = [
        cbmc,
        *spec.sources.editable,
        verification.harness,
        *ctx.include_args(),
        f"-I{os.path.abspath(ctx.include_dir)}",
        *forwarded_defines(spec.compile.defines).args,
        *_mode_args(verification, mode),
        *_unwind_args(verification),
    ]
    if _cbmc_accepts_unwinding_assertions(mode):
        argv.append("--unwinding-assertions")
    argv += _memory_model_args(verification)
    argv.append("--json-ui")
    return argv
