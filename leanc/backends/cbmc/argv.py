from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Literal

from leanc.backends.cbmc.compile_flags import (
    CompileFlagTranslation,
    translate_compile_options,
)
from leanc.specs.model import Spec, VerificationConfig
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
UNWINDING_ASSERTIONS = "--unwinding-assertions"
NO_SELF_LOOPS_TO_ASSUMPTIONS = "--no-self-loops-to-assumptions"
COVERAGE_CRITERION = ("--cover", "location")
MALLOC_MAY_FAIL_FLAGS = ("--malloc-may-fail", "--malloc-fail-null")

Mode = Literal["verify", "vacuity", "coverage"]
MODES: tuple[str, ...] = ("verify", "vacuity", "coverage")


def compile_translation(spec: Spec) -> CompileFlagTranslation:
    return translate_compile_options(spec.compile, spec.language.standard)


def _property_check_flags(spec: Spec) -> list[str]:
    flags = []
    for check in spec.effective_cbmc_checks():
        flag = CHECK_FLAGS.get(check)
        if flag is None:
            raise ValueError(f"no CBMC flag known for check {check!r}")
        flags.append(flag)
    return flags


def _mode_args(spec: Spec, mode: Mode) -> list[str]:
    if mode == "vacuity":
        return []
    if mode == "verify":
        return _property_check_flags(spec)
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


def cbmc_source_args(ctx: BackendContext) -> list[str]:
    compile_options = compile_translation(ctx.spec)
    return [
        *compile_options.include_args,
        *ctx.include_args(),
        f"-I{os.path.abspath(ctx.include_dir)}",
        *compile_options.macro_args,
        *compile_options.semantic_args,
    ]


def build_cbmc_argv(ctx: BackendContext, *, mode: Mode = "verify", inputs: Sequence[str]) -> list[str]:
    if mode not in MODES:
        raise ValueError(f"unknown CBMC mode {mode!r} (expected one of {', '.join(MODES)})")
    cbmc = ctx.tools.cbmc
    if not cbmc:
        raise ValueError("cbmc is not available (ctx.tools.cbmc is None)")
    if not inputs:
        raise ValueError("the CBMC run needs the preprocessed translation units")
    spec = ctx.spec
    verification = spec.verification
    argv: list[str] = [
        cbmc,
        *inputs,
        *compile_translation(spec).semantic_args,
        *_mode_args(spec, mode),
        *_unwind_args(verification),
    ]
    if _cbmc_accepts_unwinding_assertions(mode):
        argv += [UNWINDING_ASSERTIONS, NO_SELF_LOOPS_TO_ASSUMPTIONS]
    argv += _memory_model_args(verification)
    argv.append("--json-ui")
    return argv
