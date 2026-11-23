from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

from leanc.backends.cbmc.argv import VACUITY_DEFINE, cbmc_source_args
from leanc.backends.inductive.problems import BuildProblem, tool_error
from leanc.backends.inductive.steps import ToolSteps
from leanc.lang.c_lexer import CSource
from leanc.rules.preprocessor import has_linemarker_for
from leanc.toolchain.context import BackendContext, leanc_runtime_dir
from leanc.toolchain.cprover import CproverTool

CBMC_BANNER_PREFIX = "CBMC version"
PREPROCESS_FLAG = "--preprocess"
INDUCTIVE_MODELS_FILE = "inductive_models.c"


class UnitRole(str, Enum):
    MODELS = "models"
    HARNESS = "harness"
    EDITABLE = "editable"


@dataclass(frozen=True)
class PreprocessedUnit:
    role: UnitRole
    source: str
    text: str
    vacuity: bool = False


def inductive_models_path() -> str:
    return os.path.realpath(leanc_runtime_dir() / INDUCTIVE_MODELS_FILE)


def preprocess_argv(ctx: BackendContext, source: str, *, vacuity: bool) -> list[str]:
    cbmc = ctx.tools.cbmc
    if not cbmc:
        raise ValueError("cbmc is not available (ctx.tools.cbmc is None)")
    return [cbmc, PREPROCESS_FLAG, *cbmc_source_args(ctx), *([VACUITY_DEFINE] if vacuity else []), source]


def _without_banner(stdout: str) -> str | None:
    banner, newline, text = stdout.partition("\n")
    if not newline or not banner.startswith(CBMC_BANNER_PREFIX):
        return None
    return text


def _label(role: UnitRole, source: str, vacuity: bool) -> str:
    shown = os.path.basename(source) if role is UnitRole.MODELS else source
    return f"preprocess {role.value}{' vacuity' if vacuity else ''} {shown}"


def preprocess_unit(
    steps: ToolSteps, role: UnitRole, source: str, *, vacuity: bool = False
) -> PreprocessedUnit | BuildProblem:
    run = steps.run(CproverTool.CBMC.value, _label(role, source, vacuity), preprocess_argv(steps.ctx, source, vacuity=vacuity))
    problem = steps.exit_problem(run)
    if problem is not None:
        return problem
    text = _without_banner(run.proc.stdout)
    if text is None:
        return tool_error(f"cbmc --preprocess of {source} did not start its output with a '{CBMC_BANNER_PREFIX}' line")
    if not has_linemarker_for(source, text):
        return tool_error(f"cbmc --preprocess output of {source} has no linemarker for {source} itself")
    if CSource(text).text != text:
        return tool_error(f"cbmc --preprocess output of {source} still has a line splice")
    return PreprocessedUnit(role, source, text, vacuity)
