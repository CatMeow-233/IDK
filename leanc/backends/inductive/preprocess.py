from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

from leanc.backends.cbmc.preprocess import CBMC_BANNER_PREFIX, PINNED_BUILD_TIME, output_problem, preprocess_argv, without_banner
from leanc.backends.inductive.problems import BuildProblem, tool_error
from leanc.backends.inductive.steps import ToolSteps
from leanc.lang.c_lexer import CSource
from leanc.rules.preprocessor import has_linemarker_for
from leanc.toolchain.context import BackendContext, leanc_runtime_dir
from leanc.toolchain.cprover import CproverTool

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


def _label(role: UnitRole, source: str, vacuity: bool) -> str:
    shown = os.path.basename(source) if role is UnitRole.MODELS else source
    return f"preprocess {role.value}{' vacuity' if vacuity else ''} {shown}"


def preprocess_unit(
    steps: ToolSteps, role: UnitRole, source: str, *, vacuity: bool = False
) -> PreprocessedUnit | BuildProblem:
    argv = preprocess_argv(steps.ctx, source, vacuity=vacuity)
    run = steps.run(CproverTool.CBMC.value, _label(role, source, vacuity), argv, env_extra=PINNED_BUILD_TIME)
    problem = steps.exit_problem(run)
    if problem is not None:
        return problem
    rejected = output_problem(run.proc.stdout, run.proc.stderr)
    if rejected is not None:
        return tool_error(f"cbmc --preprocess of {source} {rejected}")
    text = without_banner(run.proc.stdout)
    if text is None:
        return tool_error(f"cbmc --preprocess of {source} did not start its output with a '{CBMC_BANNER_PREFIX}' line")
    if not has_linemarker_for(source, text):
        return tool_error(f"cbmc --preprocess output of {source} has no linemarker for {source} itself")
    if CSource(text).text != text:
        return tool_error(f"cbmc --preprocess output of {source} still has a line splice")
    return PreprocessedUnit(role, source, text, vacuity)
