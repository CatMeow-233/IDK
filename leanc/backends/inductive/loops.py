from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass
from pathlib import Path

from leanc.backends.cbmc.bodiless import bodiless_function, is_modelled_bodiless
from leanc.backends.inductive.problems import BuildProblem, tool_error
from leanc.backends.inductive.steps import ToolSteps
from leanc.toolchain.context import BackendContext
from leanc.toolchain.cprover import CproverTool

DFCC_ENTRY = "main"
DFCC_ARGS = ("--dfcc", DFCC_ENTRY, "--apply-loop-contracts")
SHOW_LOOPS = ("--show-loops", "--json-ui")
LOOPS_KEY = "loops"
BUILTIN_LIBRARY_PREFIX = "<builtin-library-"
MESSAGE_TYPE_KEY = "messageType"
MESSAGE_TEXT_KEY = "messageText"
ERROR_MESSAGE_TYPE = "ERROR"


@dataclass(frozen=True)
class GotoLoop:
    name: str
    file: str | None
    line: int | None
    function: str | None

    def where(self) -> str:
        return f"{self.file}:{self.line}" if self.file and self.line is not None else (self.file or "?")

    def describe(self) -> str:
        return f"{self.where()} (CBMC loop {self.name})"


def _goto_instrument(ctx: BackendContext) -> str:
    goto_instrument = ctx.tools.goto_instrument
    if not goto_instrument:
        raise ValueError("goto-instrument is not available (ctx.tools.goto_instrument is None)")
    return goto_instrument


def dfcc_argv(ctx: BackendContext, program: Path, output: Path) -> list[str]:
    return [_goto_instrument(ctx), *DFCC_ARGS, str(program), str(output)]


def show_loops_argv(ctx: BackendContext, program: Path) -> list[str]:
    return [_goto_instrument(ctx), *SHOW_LOOPS, str(program)]


def _int(value: object) -> int | None:
    return int(value) if isinstance(value, str) and value.isdigit() else None


def _loop(entry: object) -> GotoLoop | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
        return None
    location = entry.get("sourceLocation")
    fields = location if isinstance(location, dict) else {}
    file = fields.get("file")
    function = fields.get("function")
    return GotoLoop(
        name=entry["name"],
        file=file if isinstance(file, str) else None,
        line=_int(fields.get("line")),
        function=function if isinstance(function, str) else None,
    )


def parse_loops(stdout: str) -> list[GotoLoop] | str:
    try:
        messages = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return f"the loop list is not valid JSON ({exc.msg} at offset {exc.pos})"
    if not isinstance(messages, list):
        return "the loop list output is not a JSON list"
    errors = [m.get(MESSAGE_TEXT_KEY) for m in messages if isinstance(m, dict) and m.get(MESSAGE_TYPE_KEY) == ERROR_MESSAGE_TYPE]
    if errors:
        return f"goto-instrument reported errors: {errors[:3]}"
    lists = [m[LOOPS_KEY] for m in messages if isinstance(m, dict) and LOOPS_KEY in m]
    if len(lists) != 1 or not isinstance(lists[0], list):
        return f"expected one {LOOPS_KEY} list, found {len(lists)}"
    loops = [_loop(entry) for entry in lists[0]]
    if any(loop is None for loop in loops):
        return "a loop entry has no name"
    return [loop for loop in loops if loop is not None]


def residual_loops(loops: list[GotoLoop], harness: str) -> list[GotoLoop]:
    harness_file = posixpath.normpath(harness)
    return [
        loop
        for loop in loops
        if not (loop.file or "").startswith(BUILTIN_LIBRARY_PREFIX)
        and (loop.file is None or posixpath.normpath(loop.file) != harness_file)
    ]


def unmodelled_bodiless(text: str) -> list[str]:
    found: list[str] = []
    for line in text.splitlines():
        function = bodiless_function(line)
        if function is not None and not is_modelled_bodiless(function) and function not in found:
            found.append(function)
    return found


def instrument(steps: ToolSteps, label: str, program: Path, output: Path) -> list[str] | BuildProblem:
    output.unlink(missing_ok=True)
    run = steps.run(CproverTool.GOTO_INSTRUMENT.value, label, dfcc_argv(steps.ctx, program, output))
    problem = steps.exit_problem(run)
    if problem is not None:
        return problem
    if not output.is_file():
        return tool_error(f"inductive step '{label}' wrote no instrumented program")
    return unmodelled_bodiless(run.proc.stdout + "\n" + run.proc.stderr)


def listed_loops(steps: ToolSteps, label: str, program: Path) -> list[GotoLoop] | BuildProblem:
    run = steps.run(CproverTool.GOTO_INSTRUMENT.value, label, show_loops_argv(steps.ctx, program))
    problem = steps.exit_problem(run)
    if problem is not None:
        return problem
    loops = parse_loops(run.proc.stdout)
    if isinstance(loops, str):
        return tool_error(f"inductive step '{label}': {loops}")
    return loops
