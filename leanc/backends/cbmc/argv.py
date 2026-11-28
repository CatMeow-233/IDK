from __future__ import annotations

import os
import re
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
COVERAGE_CRITERION = ("--cover", "location")
MALLOC_MAY_FAIL_FLAGS = ("--malloc-may-fail", "--malloc-fail-null")
NO_MALLOC_MAY_FAIL_FLAG = "--no-malloc-may-fail"
CBMC_DEFAULT_MALLOC_MAY_FAIL_MAJOR = 6
_CBMC_MAJOR_RE = re.compile(r"^\s*(\d+)\.")

Mode = Literal["verify", "vacuity", "coverage"]
MODES: tuple[str, ...] = ("verify", "vacuity", "coverage")


def cbmc_major(version: str | None) -> int | None:
    match = _CBMC_MAJOR_RE.match(version or "")
    return int(match.group(1)) if match else None


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
        return [VACUITY_DEFINE]
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


def _memory_model_args(verification: VerificationConfig, cbmc_version: str | None = None) -> list[str]:
    args: list[str] = []
    if verification.malloc_may_fail:
        args += MALLOC_MAY_FAIL_FLAGS
    elif (cbmc_major(cbmc_version) or 0) >= CBMC_DEFAULT_MALLOC_MAY_FAIL_MAJOR:
        args.append(NO_MALLOC_MAY_FAIL_FLAG)
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
    compile_options = compile_translation(spec)
    argv: list[str] = [
        cbmc,
        *spec.sources.editable,
        verification.harness,
        *compile_options.include_args,
        *ctx.include_args(),
        f"-I{os.path.abspath(ctx.include_dir)}",
        *compile_options.macro_args,
        *compile_options.semantic_args,
        *_mode_args(spec, mode),
        *_unwind_args(verification),
    ]
    if _cbmc_accepts_unwinding_assertions(mode):
        argv.append("--unwinding-assertions")
    argv += _memory_model_args(verification, ctx.tools.cbmc_version)
    argv.append("--json-ui")
    return argv
