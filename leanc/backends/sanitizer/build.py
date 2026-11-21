"""Sanitizer build command and a small, tolerant parser that explains why the sanitized build failed."""

from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path

from leanc.backends.clang import DIAGNOSTIC_FLAGS, define_args
from leanc.backends.sanitizer.plan import SanitizerPlan
from leanc.foundation.models import Diagnostic, Location, PathStep
from leanc.toolchain.context import BackendContext
from leanc.toolchain.cxx import compiler

BASE_FLAGS: tuple[str, ...] = ("-g", "-O1", "-fno-omit-frame-pointer")
BUILD_TOOL = "clang"
UNDEFINED_REFERENCE_RULE = "undefined-reference"
MULTIPLE_DEFINITION_RULE = "multiple-definition"
LINK_ERROR_RULE = "link-error"
DRIVER_RULE = "driver"
TEMPORARY_DIR = "/tmp/"

_LEVELS = r"fatal error|error|warning|note"
_ERROR_LEVELS = ("error", "fatal error")
_COMPILER_DIAGNOSTIC_RE = re.compile(
    rf"^(?P<file>[^\s:][^:]*?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<sev>{_LEVELS}):\s*(?P<msg>.*?)\s*$"
)
_WARNING_OPTION_RE = re.compile(r"^(?P<msg>.*?)\s*\[(?P<opt>-W[^\[\]]*)\]$")
_LINKER_PROGRAM = r"(?:\S*/)?(?:ld|ld\.[A-Za-z0-9_]+|lld|collect2)"
_LINKER_FUNCTION_RE = re.compile(rf"^(?:{_LINKER_PROGRAM}:\s+)?(?P<obj>.+?): in function [`'‘](?P<func>[^'`’]+)['’]:\s*$")
_LINKER_REFERENCE_RE = re.compile(
    rf"^(?:{_LINKER_PROGRAM}:\s+)?(?P<where>.*?):\s*(?P<msg>(?:undefined reference to|multiple definition of)\s.*?)\s*$"
)
_LINKER_SECTION_RE = re.compile(r"^(?P<file>[^:(]+?)(?::(?P<line>\d+))?:\(")
_DRIVER_MESSAGE_RE = re.compile(
    rf"^(?P<prog>(?:\S*/)?clang(?:\+\+)?(?:-\d+)?|{_LINKER_PROGRAM}):\s*(?:(?P<sev>fatal error|error|warning):\s*)?"
    r"(?P<msg>.+?)\s*$"
)
_COMPILER_CRASH_RE = re.compile(
    r"PLEASE submit a bug report|clang frontend command failed due to signal|unable to execute command|^Stack dump:",
    re.MULTILINE,
)


def build_command(ctx: BackendContext, binary: Path, plan: SanitizerPlan) -> list[str]:
    """argv building the sanitized test binary (cwd = snapshot, relative sources)."""
    spec = ctx.spec
    language_driver = compiler(ctx)
    assert language_driver and spec.tests.driver
    return [
        language_driver,
        f"-std={spec.language.standard}",
        *BASE_FLAGS,
        *plan.compile_flags,
        *spec.compile.flags,
        *define_args(spec.compile.defines),
        *ctx.include_args(),
        f"-I{os.path.realpath(ctx.include_dir)}",
        *DIAGNOSTIC_FLAGS,
        *spec.sources.editable,
        spec.tests.driver,
        "-o",
        str(binary),
    ]


def compiler_crash_reported(stderr: str) -> bool:
    return bool(_COMPILER_CRASH_RE.search(stderr))


def _is_clang_temporary_object(path: str) -> bool:
    return path.endswith(".o") and (path.startswith(TEMPORARY_DIR) or "/" not in path)


def _build_path(path: str, base_dir: str | None) -> str | None:
    stripped = path.strip()
    prefix = base_dir.rstrip("/") + "/" if base_dir else None
    if prefix and stripped.startswith(prefix):
        return posixpath.normpath(stripped[len(prefix) :])
    if _is_clang_temporary_object(stripped):
        return None
    return stripped


def _error_or_level(level: str) -> str:
    return "error" if level in _ERROR_LEVELS else level


def _is_linker_program(program: str) -> bool:
    return "ld" in program.rsplit("/", 1)[-1]


def _linker_reference(match: re.Match, line: str, function: str | None, base_dir: str | None) -> Diagnostic:
    where = match["where"].strip()
    section = _LINKER_SECTION_RE.match(where)
    file = _build_path(section["file"] if section else where, base_dir) if where else None
    line_number = int(section["line"]) if section and section["line"] else None
    location = Location(file=file, line=line_number, function=function) if (file or function) else None
    message = match["msg"]
    rule = UNDEFINED_REFERENCE_RULE if message.startswith("undefined") else MULTIPLE_DEFINITION_RULE
    return Diagnostic(tool=BUILD_TOOL, rule_id=rule, severity="error", message=message, location=location, raw=line)


def _add_compiler_diagnostic(diagnostics: list[Diagnostic], match: re.Match, line: str, base_dir: str | None) -> None:
    severity = _error_or_level(match["sev"])
    message, rule = match["msg"], None
    if option := _WARNING_OPTION_RE.match(message):
        message, rule = option["msg"], option["opt"].split(",")[-1]
    location = Location(
        file=_build_path(match["file"], base_dir),
        line=int(match["line"]),
        column=int(match["col"]) if match["col"] else None,
    )
    if severity == "note" and diagnostics:
        previous = diagnostics[-1]
        previous.path.append(PathStep(location=location, message=message))
        previous.raw = (previous.raw or "") + "\n" + line
        return
    diagnostics.append(
        Diagnostic(tool=BUILD_TOOL, rule_id=rule, severity=severity, message=message, location=location, raw=line)
    )


def _driver_diagnostic(match: re.Match, line: str) -> Diagnostic:
    severity = _error_or_level(match["sev"] or "error")
    message = match["msg"]
    is_link_failure = "linker command failed" in message or _is_linker_program(match["prog"])
    rule = LINK_ERROR_RULE if is_link_failure else DRIVER_RULE
    return Diagnostic(tool=BUILD_TOOL, rule_id=rule, severity=severity, message=message, raw=line)


def parse_build_diagnostics(text: str, *, base_dir: str | None = None) -> list[Diagnostic]:
    """Compiler and linker messages of the sanitizer build; notes become path steps of the preceding diagnostic."""
    diagnostics: list[Diagnostic] = []
    linker_function: str | None = None
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if context := _LINKER_FUNCTION_RE.match(line):
            linker_function = context["func"]
        elif reference := _LINKER_REFERENCE_RE.match(line):
            diagnostics.append(_linker_reference(reference, line, linker_function, base_dir))
        elif compiler_message := _COMPILER_DIAGNOSTIC_RE.match(line):
            _add_compiler_diagnostic(diagnostics, compiler_message, line, base_dir)
        elif driver_message := _DRIVER_MESSAGE_RE.match(line):
            diagnostics.append(_driver_diagnostic(driver_message, line))
    return diagnostics
