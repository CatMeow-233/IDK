from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from pathlib import Path

from leanc.backends.cbmc.argv import Mode, compile_translation
from leanc.backends.cbmc.execution import STAGE, TOOL, ToolProblem, auxiliary_run_problem, snapshot_cwd
from leanc.backends.cbmc.preprocess import CBMC_BANNER_PREFIX, PINNED_BUILD_TIME, output_problem, preprocess_argv, without_banner
from leanc.backends.cbmc.symbol_table import CPROVER_DECLARATION_RULE, SHOW_SYMBOL_TABLE, cprover_declarations, parse_symbol_table
from leanc.backends.cbmc.vacuity_divergence import DIVERGENCE_RULE, DIVERGENCE_RULE_ID, Divergence, ReachSite, reach_sites, vacuity_divergence
from leanc.foundation.models import Diagnostic, Location, ToolInvocation
from leanc.foundation.runner import Limits
from leanc.foundation.status import Outcome
from leanc.rules.preprocessor import has_linemarker_for
from leanc.toolchain.context import BackendContext

__all__ = ["BUILD_SUBDIR", "PREPROCESSED_SUFFIX", "ProgramInputs", "cprover_declaration_problem", "prepare_inputs"]

BUILD_SUBDIR = "cbmc"
PREPROCESSED_SUFFIX = ".i"
WITH_DEFINE = " with LEANC_VACUITY_CHECK"
VERIFY_DIR = "verify"
VACUITY_DIR = "vacuity"
VACUITY_MODE: Mode = "vacuity"
PARENT_DIR = ".."
EDITABLE_ROLE = "editable source"
HARNESS_ROLE = "harness"
SYMBOL_TABLE_LOG = "symbol table"
CPROVER_DECLARATION_RULE_ID = "cprover-declaration"
MAX_LISTED_DECLARATIONS = 5


@dataclass(frozen=True)
class ProgramInputs:
    verify: tuple[str, ...]
    vacuity: tuple[str, ...]
    sites: tuple[ReachSite, ...]

    @property
    def declaring_units(self) -> tuple[str, ...]:
        return self.vacuity or self.verify

    def for_mode(self, mode: Mode) -> tuple[str, ...]:
        if mode != VACUITY_MODE:
            return self.verify
        if not self.vacuity:
            raise ValueError("the vacuity run was not prepared: no vacuity translation units")
        return self.vacuity


@dataclass(frozen=True)
class _Unit:
    role: str
    source: str
    target: str
    verify: str
    vacuity: str | None


def _what(source: str, vacuity: bool) -> str:
    return f"cbmc --preprocess of {source}{WITH_DEFINE if vacuity else ''}"


def _preprocessed(
    ctx: BackendContext, limits: Limits, source: str, vacuity: bool, invocations: list[ToolInvocation]
) -> str | ToolProblem:
    what = _what(source, vacuity)
    argv = preprocess_argv(ctx, source, vacuity=vacuity)
    proc = ctx.run(argv, limits=limits, cwd=snapshot_cwd(ctx), env_extra=PINNED_BUILD_TIME)
    log_name = ctx.log_name(STAGE, f"preprocess{WITH_DEFINE if vacuity else ''} {source}")
    invocations.append(proc.to_invocation(TOOL, ctx.run_dir, log_name))
    problem = auxiliary_run_problem(what, proc, limits)
    if problem is not None:
        return problem
    rejected = output_problem(proc.stdout, proc.stderr)
    if rejected is not None:
        return ToolProblem(Outcome.ERROR, f"{what} {rejected}")
    text = without_banner(proc.stdout)
    if text is None:
        return ToolProblem(Outcome.ERROR, f"{what} did not start its output with a '{CBMC_BANNER_PREFIX}' line")
    if not has_linemarker_for(source, text):
        return ToolProblem(Outcome.ERROR, f"{what} printed no linemarker for {source} itself")
    return text


def _divergence_problem(role: str, source: str, divergence: Divergence) -> ToolProblem:
    location = Location(file=divergence.origin.file, line=divergence.origin.line) if divergence.origin else Location(file=source)
    summary = f"the {role} {source} {DIVERGENCE_RULE}: {divergence.describe()}"
    return ToolProblem(
        Outcome.ERROR,
        summary,
        [Diagnostic(tool=TOOL, rule_id=DIVERGENCE_RULE_ID, severity="error", message=summary, location=location)],
    )


def _target(source: str) -> str | None:
    stem = posixpath.splitext(posixpath.normpath(source))[0]
    if posixpath.isabs(stem) or stem == PARENT_DIR or stem.startswith(PARENT_DIR + posixpath.sep):
        return None
    return stem + PREPROCESSED_SUFFIX


def _unit(
    ctx: BackendContext, limits: Limits, role: str, source: str, with_vacuity: bool, invocations: list[ToolInvocation]
) -> _Unit | ToolProblem:
    target = _target(source)
    if target is None:
        return ToolProblem(Outcome.ERROR, f"the {role} {source} is outside the project, so its preprocessed text has no place")
    verify = _preprocessed(ctx, limits, source, False, invocations)
    if isinstance(verify, ToolProblem):
        return verify
    if not with_vacuity:
        return _Unit(role, source, target, verify, None)
    vacuity = _preprocessed(ctx, limits, source, True, invocations)
    if isinstance(vacuity, ToolProblem):
        return vacuity
    divergence = vacuity_divergence(verify, vacuity)
    if divergence is not None:
        return _divergence_problem(role, source, divergence)
    return _Unit(role, source, target, verify, vacuity)


def _write(root: Path, target: str, text: str) -> str:
    path = root / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _collision(units: list[_Unit]) -> ToolProblem | None:
    seen: dict[str, str] = {}
    for unit in units:
        if unit.target in seen:
            return ToolProblem(
                Outcome.ERROR,
                f"{seen[unit.target]} and {unit.source} would share the preprocessed file {unit.target}; rename one",
            )
        seen[unit.target] = unit.source
    return None


def prepare_inputs(
    ctx: BackendContext, limits: Limits, invocations: list[ToolInvocation], *, with_vacuity: bool
) -> ProgramInputs | ToolProblem:
    sources = [*((EDITABLE_ROLE, rel) for rel in ctx.spec.sources.editable), (HARNESS_ROLE, ctx.spec.verification.harness)]
    units: list[_Unit] = []
    for role, source in sources:
        unit = _unit(ctx, limits, role, source, with_vacuity, invocations)
        if isinstance(unit, ToolProblem):
            return unit
        units.append(unit)
    collision = _collision(units)
    if collision is not None:
        return collision
    root = Path(os.path.abspath(ctx.build_dir)) / BUILD_SUBDIR
    verify = tuple(_write(root / VERIFY_DIR, unit.target, unit.verify) for unit in units)
    vacuity = tuple(_write(root / VACUITY_DIR, unit.target, unit.vacuity) for unit in units if unit.vacuity is not None)
    sites = tuple(site for unit in units if unit.vacuity is not None for site in reach_sites(unit.vacuity))
    return ProgramInputs(verify, vacuity, sites)


def _declaration_problem(declared: list[str]) -> ToolProblem:
    shown = "; ".join(declared[:MAX_LISTED_DECLARATIONS])
    more = f" (+{len(declared) - MAX_LISTED_DECLARATIONS} more)" if len(declared) > MAX_LISTED_DECLARATIONS else ""
    summary = f"the verified program (harness, editable sources, included headers) {CPROVER_DECLARATION_RULE}: {shown}{more}; no verdict is possible"
    return ToolProblem(
        Outcome.ERROR,
        summary,
        [Diagnostic(tool=TOOL, rule_id=CPROVER_DECLARATION_RULE_ID, severity="error", message=item) for item in declared],
    )


def cprover_declaration_problem(
    ctx: BackendContext, limits: Limits, inputs: ProgramInputs, invocations: list[ToolInvocation]
) -> ToolProblem | None:
    cbmc = ctx.tools.cbmc
    if not cbmc:
        raise ValueError("cbmc is not available (ctx.tools.cbmc is None)")
    argv = [cbmc, *inputs.declaring_units, *compile_translation(ctx.spec).semantic_args, *SHOW_SYMBOL_TABLE]
    proc = ctx.run(argv, limits=limits, cwd=snapshot_cwd(ctx))
    invocations.append(proc.to_invocation(TOOL, ctx.run_dir, ctx.log_name(STAGE, SYMBOL_TABLE_LOG)))
    what = "cbmc --show-symbol-table of the preprocessed program"
    problem = auxiliary_run_problem(what, proc, limits)
    if problem is not None:
        return problem
    table = parse_symbol_table(proc.stdout)
    if isinstance(table, str):
        return ToolProblem(Outcome.ERROR, f"{what}: {table}")
    declared = cprover_declarations(table)
    return _declaration_problem(declared) if declared else None
