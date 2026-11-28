from __future__ import annotations

from leanc.backends.cbmc.argv import VACUITY_DEFINE, cbmc_source_args
from leanc.toolchain.context import BackendContext

__all__ = [
    "CBMC_BANNER_PREFIX",
    "NOT_UTF8_OUTPUT",
    "PINNED_BUILD_TIME",
    "PREPROCESSING_ERROR_MARKER",
    "PREPROCESS_FLAG",
    "SILENT_PREPROCESSING_FAILURE",
    "output_problem",
    "preprocess_argv",
    "without_banner",
]

CBMC_BANNER_PREFIX = "CBMC version"
PREPROCESS_FLAG = "--preprocess"
PINNED_BUILD_TIME = {"SOURCE_DATE_EPOCH": "0"}
PREPROCESSING_ERROR_MARKER = "PREPROCESSING ERROR"
REPLACEMENT_CHARACTER = "\ufffd"
STDERR_EXCERPT_LINES = 3
SILENT_PREPROCESSING_FAILURE = (
    f"reported {PREPROCESSING_ERROR_MARKER} despite exit code 0, so its text is not the whole translation unit"
)
NOT_UTF8_OUTPUT = (
    "printed text that is not valid UTF-8 (or holds U+FFFD), so the .i file written from it would not hold the compiled bytes"
)


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


def _stderr_excerpt(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return "; ".join(lines[:STDERR_EXCERPT_LINES])


def output_problem(stdout: str, stderr: str) -> str | None:
    if any(line.strip() == PREPROCESSING_ERROR_MARKER for line in stderr.splitlines()):
        return f"{SILENT_PREPROCESSING_FAILURE}: {_stderr_excerpt(stderr)}"
    if REPLACEMENT_CHARACTER in stdout:
        return NOT_UTF8_OUTPUT
    return None
