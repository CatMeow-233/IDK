from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from leanc.backends.cbmc.bodiless import undeclared_function
from leanc.backends.cbmc.compile_flags import cbmc_preprocessor_macro_args
from leanc.backends.inductive.problems import BuildProblem, tool_error
from leanc.backends.inductive.steps import StepRun, ToolSteps
from leanc.backends.inductive.symbols import SymbolTable, parse_symbol_table
from leanc.foundation.models import Location
from leanc.toolchain.context import BackendContext
from leanc.toolchain.cprover import CproverTool

GOTO_CC_VERBOSITY = ("--verbosity", "2")
OBJECT_BITS_FLAG = "--object-bits"
OUTPUT_FLAG = "-o"
SHOW_SYMBOL_TABLE = ("--show-symbol-table", "--json-ui")
_DIAGNOSTIC_LOCATION_RE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+)(?::\d+)?: ")


@dataclass(frozen=True)
class Undeclared:
    function: str
    location: Location | None


def goto_cc_argv(ctx: BackendContext, output: Path, units: Sequence[Path]) -> list[str]:
    goto_cc = ctx.tools.goto_cc
    if not goto_cc:
        raise ValueError("goto-cc is not available (ctx.tools.goto_cc is None)")
    spec = ctx.spec
    object_bits = spec.verification.object_bits if spec.verification else None
    return [
        goto_cc,
        *GOTO_CC_VERBOSITY,
        *cbmc_preprocessor_macro_args(spec.compile, spec.language.standard),
        *([OBJECT_BITS_FLAG, str(object_bits)] if object_bits else []),
        OUTPUT_FLAG,
        str(output),
        *(str(unit) for unit in units),
    ]


def symbol_table_argv(ctx: BackendContext, program: Path) -> list[str]:
    goto_instrument = ctx.tools.goto_instrument
    if not goto_instrument:
        raise ValueError("goto-instrument is not available (ctx.tools.goto_instrument is None)")
    return [goto_instrument, *SHOW_SYMBOL_TABLE, str(program)]


def _location(line: str) -> Location | None:
    match = _DIAGNOSTIC_LOCATION_RE.match(line)
    return Location(file=match.group("file"), line=int(match.group("line"))) if match else None


def undeclared_functions(run: StepRun) -> list[Undeclared]:
    found: dict[str, Undeclared] = {}
    for line in (run.proc.stdout + "\n" + run.proc.stderr).splitlines():
        function = undeclared_function(line)
        if function is not None and function not in found:
            found[function] = Undeclared(function, _location(line.strip()))
    return list(found.values())


def compile_units(steps: ToolSteps, label: str, output: Path, units: Sequence[Path]) -> StepRun:
    return steps.run(CproverTool.GOTO_CC.value, label, goto_cc_argv(steps.ctx, output, units))


def read_symbol_table(steps: ToolSteps, label: str, program: Path) -> SymbolTable | BuildProblem:
    run = steps.run(CproverTool.GOTO_INSTRUMENT.value, label, symbol_table_argv(steps.ctx, program))
    problem = steps.exit_problem(run)
    if problem is not None:
        return problem
    table = parse_symbol_table(run.proc.stdout)
    if isinstance(table, str):
        return tool_error(f"inductive step '{label}': {table}")
    return table
