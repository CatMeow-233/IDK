from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from leanc.backends.sanitizer.frames import project_relative_path
from leanc.foundation.errors import JSON_LOAD_ERRORS
from leanc.foundation.models import Location, ToolInvocation
from leanc.foundation.runner import Limits
from leanc.toolchain.context import BackendContext

STAGE = "sanitizer"
SYMBOLIZER_TOOL = "llvm-symbolizer"
SYMBOLIZER_TIMEOUT_S = 30.0
MAX_SYMBOLIZED_ADDRESSES = 256
MAX_IMAGE_OFFSET = 1 << 40
RETURN_ADDRESS_ADJUSTMENT = 1


@dataclass(frozen=True)
class CodeAddress:
    offset: int
    is_return_address: bool

    @property
    def lookup(self) -> int:
        return self.offset - RETURN_ADDRESS_ADJUSTMENT if self.is_return_address else self.offset


@dataclass
class Symbolized:
    locations: dict[CodeAddress, Location] = field(default_factory=dict)
    functions: dict[CodeAddress, Location] = field(default_factory=dict)
    problem: str | None = None
    invocations: list[ToolInvocation] = field(default_factory=list)


def _innermost_symbol(entry: object) -> dict | None:
    if not isinstance(entry, dict):
        return None
    symbols = entry.get("Symbol")
    if not isinstance(symbols, list) or not symbols or not isinstance(symbols[0], dict):
        return None
    return symbols[0]


def _project_location(symbol: dict, file_key: str, line_key: str, project_files: set[str], base_dir: str) -> Location | None:
    file_name, line, function = symbol.get(file_key), symbol.get(line_key), symbol.get("FunctionName")
    if not isinstance(file_name, str) or not isinstance(line, int) or line <= 0 or not isinstance(function, str):
        return None
    rel = project_relative_path(file_name, project_files, base_dir)
    if rel is None:
        return None
    column = symbol.get("Column")
    return Location(file=rel, line=line, column=column if isinstance(column, int) and column > 0 else None, function=function)


def parse_symbolizer_output(
    text: str, addresses: list[CodeAddress], project_files: set[str], base_dir: str
) -> Symbolized:
    result = Symbolized()
    try:
        entries = json.loads(text)
    except JSON_LOAD_ERRORS as exc:
        result.problem = f"llvm-symbolizer output is not JSON: {exc}"
        return result
    if not isinstance(entries, list) or len(entries) != len(addresses):
        result.problem = "llvm-symbolizer returned an unexpected number of entries"
        return result
    for address, entry in zip(addresses, entries):
        symbol = _innermost_symbol(entry)
        if symbol is None:
            continue
        location = _project_location(symbol, "FileName", "Line", project_files, base_dir)
        if location is not None:
            result.locations[address] = location
        function = _project_location(symbol, "StartFileName", "StartLine", project_files, base_dir)
        if function is not None:
            result.functions[address] = Location(file=function.file, line=function.line, function=function.function)
    return result


def symbolize(
    ctx: BackendContext, symbolizer: str, binary: Path, addresses: list[CodeAddress], project_files: set[str]
) -> Symbolized:
    wanted = sorted(set(addresses), key=lambda a: (a.offset, a.is_return_address))
    if len(wanted) > MAX_SYMBOLIZED_ADDRESSES or any(not 0 < a.lookup < MAX_IMAGE_OFFSET for a in wanted):
        return Symbolized(problem="trace addresses are out of range for the trace binary")
    if not wanted:
        return Symbolized()
    argv = [symbolizer, f"--obj={binary}", "--output-style=JSON", *(hex(a.lookup) for a in wanted)]
    proc = ctx.run(argv, limits=Limits(timeout_s=ctx.timeout(SYMBOLIZER_TIMEOUT_S)), cwd=ctx.snapshot_dir)
    invocation = proc.to_invocation(SYMBOLIZER_TOOL, ctx.run_dir, ctx.log_name(STAGE, "ownership symbolize"))
    if not proc.ok:
        return Symbolized(problem=f"llvm-symbolizer failed ({proc.error or f'exit {proc.exit_code}'})", invocations=[invocation])
    result = parse_symbolizer_output(proc.stdout, wanted, project_files, os.path.realpath(ctx.snapshot_dir))
    result.invocations.append(invocation)
    return result
