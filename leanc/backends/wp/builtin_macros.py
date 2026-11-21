from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from leanc.backends.wp.argv import MACHDEP
from leanc.backends.wp.installation import extra_read_only_mounts, tool_path
from leanc.foundation.models import ToolInvocation
from leanc.foundation.redact import redacted_excerpt
from leanc.foundation.runner import Limits
from leanc.toolchain.context import BackendContext
from leanc.toolchain.framac import INSTALL_HINT, find_framac

BUILTIN_MACROS_DIR = "frama-c-machdep"
BUILTIN_MACROS_HEADER = "__fc_builtin_macros.h"
PRINT_BUILTIN_MACROS_ARGS: tuple[str, ...] = ("-machdep", MACHDEP, "-print-machdep-builtin-macros")
FRAMA_C_TOOL = "frama-c"
FRAMA_C_MISSING = (
    "frama-c not found: the predefined macros of Frama-C's preprocessing cannot be reproduced for the wp gate "
    f"({INSTALL_HINT})"
)


def builtin_macros_path(build_dir: Path) -> Path:
    return build_dir / BUILTIN_MACROS_DIR / BUILTIN_MACROS_HEADER


@dataclass
class BuiltinMacros:
    path: Path
    problems: list[str] = field(default_factory=list)
    timeouts: list[str] = field(default_factory=list)
    invocations: list[ToolInvocation] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return not self.problems and not self.timeouts


def materialize_builtin_macros(ctx: BackendContext, limits: Limits, stage: str) -> BuiltinMacros:
    built = BuiltinMacros(builtin_macros_path(ctx.build_dir))
    fc = find_framac()
    if fc is None:
        built.problems.append(FRAMA_C_MISSING)
        return built
    argv = [fc.frama_c, *PRINT_BUILTIN_MACROS_ARGS]
    proc = ctx.run(
        argv,
        limits=limits,
        env_extra={"PATH": tool_path(fc)},
        mounts=ctx.mounts(extra_ro=extra_read_only_mounts(fc, [])),
    )
    built.invocations.append(proc.to_invocation(FRAMA_C_TOOL, ctx.run_dir, ctx.log_name(stage, "builtin-macros")))
    if proc.timed_out:
        built.timeouts.append("printing Frama-C's machdep builtin macros timed out")
    elif not proc.ok or proc.output_truncated or not proc.stdout.strip():
        reason = proc.error or f"exit {proc.exit_code}"
        built.problems.append(
            f"frama-c could not print its machdep builtin macros ({reason}): {redacted_excerpt(proc.stderr)}"
        )
    else:
        built.path.parent.mkdir(parents=True, exist_ok=True)
        built.path.write_text(proc.stdout, encoding="utf-8")
    return built
