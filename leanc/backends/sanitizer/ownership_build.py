from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from leanc.backends.clang import DIAGNOSTIC_FLAGS
from leanc.backends.sanitizer.build import BASE_FLAGS, instrumented_compile_flags
from leanc.backends.sanitizer.plan import SanitizerPlan
from leanc.foundation.models import ToolInvocation
from leanc.foundation.runner import Limits
from leanc.foundation.text import safe_name
from leanc.toolchain.context import BackendContext, leanc_runtime_dir
from leanc.toolchain.cxx import compiler

STAGE = "sanitizer"
BUILD_TOOL = "ownership-trace-build"
TRACE_BINARY_NAME = "trace_bin"
RUNTIME_SOURCE = "ownership_trace.c"
RUNTIME_STANDARD = "-std=c17"
INSTRUMENT_FLAGS: tuple[str, ...] = ("-finstrument-functions",)
POSITION_INDEPENDENT_COMPILE = "-fPIE"
POSITION_INDEPENDENT_LINK = "-pie"
WRAPPED_ALLOCATORS: tuple[str, ...] = ("malloc", "calloc", "realloc", "free")


@dataclass
class TraceBuild:
    binary: Path | None = None
    problem: str | None = None
    invocations: list[ToolInvocation] = field(default_factory=list)


def wrap_flags() -> list[str]:
    return [f"-Wl,--wrap={name}" for name in WRAPPED_ALLOCATORS]


def _project_compile_argv(ctx: BackendContext, plan: SanitizerPlan, source: str, obj: Path, instrumented: bool) -> list[str]:
    return [
        compiler(ctx),
        *instrumented_compile_flags(ctx, plan),
        *(INSTRUMENT_FLAGS if instrumented else ()),
        POSITION_INDEPENDENT_COMPILE,
        "-c",
        source,
        "-o",
        str(obj),
    ]


def _runtime_compile_argv(ctx: BackendContext, plan: SanitizerPlan, runtime: Path, obj: Path) -> list[str]:
    return [
        compiler(ctx),
        RUNTIME_STANDARD,
        *BASE_FLAGS,
        *plan.compile_flags,
        *DIAGNOSTIC_FLAGS,
        POSITION_INDEPENDENT_COMPILE,
        "-c",
        str(runtime),
        "-o",
        str(obj),
    ]


def _object(obj_dir: Path, index: int, rel: str) -> Path:
    return obj_dir / f"{index:02d}-{safe_name(rel)}.o"


def build_steps(ctx: BackendContext, plan: SanitizerPlan, out_root: Path) -> tuple[list[tuple[str, list[str]]], Path]:
    spec = ctx.spec
    obj_dir = out_root / "obj"
    runtime = Path(os.path.realpath(leanc_runtime_dir() / RUNTIME_SOURCE))
    steps: list[tuple[str, list[str]]] = []
    objects: list[str] = []
    for index, rel in enumerate(spec.sources.editable):
        obj = _object(obj_dir, index, rel)
        steps.append((f"compile {rel}", _project_compile_argv(ctx, plan, rel, obj, instrumented=True)))
        objects.append(str(obj))
    driver_obj = _object(obj_dir, len(spec.sources.editable), spec.tests.driver or "driver")
    steps.append((f"compile {spec.tests.driver}", _project_compile_argv(ctx, plan, spec.tests.driver or "", driver_obj, instrumented=False)))
    runtime_obj = obj_dir / "runtime.o"
    steps.append(("compile runtime", _runtime_compile_argv(ctx, plan, runtime, runtime_obj)))
    binary = out_root / TRACE_BINARY_NAME
    link = [
        compiler(ctx),
        *spec.compile.flags,
        *plan.compile_flags,
        *objects,
        str(driver_obj),
        str(runtime_obj),
        *wrap_flags(),
        POSITION_INDEPENDENT_LINK,
        "-o",
        str(binary),
    ]
    steps.append(("link", link))
    return steps, binary


def build_trace_binary(ctx: BackendContext, plan: SanitizerPlan, out_root: Path) -> TraceBuild:
    if out_root.exists():
        shutil.rmtree(out_root)
    (out_root / "obj").mkdir(parents=True)
    steps, binary = build_steps(ctx, plan, out_root)
    build = TraceBuild()
    limits = Limits(timeout_s=ctx.timeout(ctx.spec.compile.timeout_s))
    mounts = ctx.mounts(extra_ro=[os.path.realpath(leanc_runtime_dir())])
    for label, argv in steps:
        proc = ctx.run(argv, limits=limits, cwd=ctx.snapshot_dir, mounts=mounts)
        build.invocations.append(proc.to_invocation(BUILD_TOOL, ctx.run_dir, ctx.log_name(STAGE, f"ownership {label}")))
        if not proc.ok:
            reason = proc.error or ("timed out" if proc.timed_out else f"exit {proc.exit_code}")
            build.problem = f"trace build step '{label}' failed ({reason})"
            return build
    if not binary.is_file():
        build.problem = "trace build produced no binary"
        return build
    build.binary = binary
    return build
