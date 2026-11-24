from __future__ import annotations

from leanc.backends.cbmc.argv import VACUITY_DEFINE, cbmc_source_args
from leanc.toolchain.context import BackendContext

__all__ = ["CBMC_BANNER_PREFIX", "PINNED_BUILD_TIME", "PREPROCESS_FLAG", "preprocess_argv", "without_banner"]

CBMC_BANNER_PREFIX = "CBMC version"
PREPROCESS_FLAG = "--preprocess"
PINNED_BUILD_TIME = {"SOURCE_DATE_EPOCH": "0"}


def preprocess_argv(ctx: BackendContext, source: str, *, vacuity: bool) -> list[str]:
    cbmc = ctx.tools.cbmc
    if not cbmc:
        raise ValueError("cbmc is not available (ctx.tools.cbmc is None)")
    return [cbmc, PREPROCESS_FLAG, *cbmc_source_args(ctx), *([VACUITY_DEFINE] if vacuity else []), source]


def without_banner(stdout: str) -> str | None:
    banner, newline, text = stdout.partition("\n")
    if not newline or not banner.startswith(CBMC_BANNER_PREFIX):
        return None
    return text
