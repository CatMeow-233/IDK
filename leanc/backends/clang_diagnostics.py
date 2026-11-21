"""Parse clang driver, frontend, analyzer-text and linker output into diagnostics, and recognise compiler crashes."""

from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

from leanc.backends.termination import signal_from_exit_status
from leanc.foundation.models import Diagnostic, Location, PathStep, Severity

LINKER_ERROR_RULE = "linker-error"
LINKER_WARNING_RULE = "linker-warning"
WERROR_FLAG = "-Werror"
WERROR_PREFIX = "-Werror="

_CLANG_LEVELS = r"fatal error|error|warning|note|remark"
_SEVERITY_BY_LEVEL: dict[str, Severity] = {
    "fatal error": "error",
    "error": "error",
    "warning": "warning",
    "note": "note",
    "remark": "info",
}
_DEFAULT_RULE_BY_LEVEL = {
    "fatal error": "clang-fatal",
    "error": "clang-error",
    "warning": "clang-warning",
    "remark": "clang-remark",
}

_LOCATED_DIAGNOSTIC_RE = re.compile(
    rf"^(?P<file>.+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<sev>{_CLANG_LEVELS}):\s?(?P<msg>.*)$"
)
_LINELESS_DIAGNOSTIC_RE = re.compile(rf"^(?P<file>[^\s:][^:]*?):\s*(?P<sev>{_CLANG_LEVELS}):\s?(?P<msg>.*)$")
_UNPREFIXED_DIAGNOSTIC_RE = re.compile(rf"^(?P<sev>{_CLANG_LEVELS}):\s?(?P<msg>.*)$")
_TRAILING_OPTION_GROUP_RE = re.compile(r"^(?P<msg>.*?)\s*\[(?P<opt>[^\[\]]+)\]\s*$")
_ANALYZER_CHECKER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")
_DIAGNOSTIC_COUNT_SUMMARY_RE = re.compile(r"^\d+ (?:warning|error)s?(?: and \d+ (?:warning|error)s?)? generated\.?\s*$")
_INCLUDE_STACK_HEAD_RE = re.compile(r"^In file included from .+:\d+:")
_INCLUDE_STACK_CONTINUATION_RE = re.compile(r"^\s+from .+:\d+[:,]\s*$")

_LINKER_PROGRAM = r"(?:\S*/)?(?:ld|ld\.[A-Za-z0-9_]+|lld|collect2)"
_LINKER_FUNCTION_CONTEXT_RE = re.compile(
    rf"^(?:{_LINKER_PROGRAM}: )?(?P<obj>.+?): in function [`'‘](?P<func>[^'`’]+)['’]:\s*$"
)
_LINKER_SYMBOL_REFERENCE_RE = re.compile(
    rf"^(?:{_LINKER_PROGRAM}: )?(?P<where>.+?): (?P<msg>(?:undefined reference to|multiple definition of) .*)$"
)
_LINKER_SECTION_LOCATION_RE = re.compile(r"^(?P<file>.+?)(?::(?P<line>\d+))?:\(")
_LINKER_MESSAGE_RE = re.compile(rf"^(?P<prog>{_LINKER_PROGRAM}): (?:(?P<sev>fatal error|error|warning): )?(?P<msg>.*)$")
_LLD_REFERENCE_DETAIL_RE = re.compile(r"^>>>\s*(?P<msg>.*)$")

_TOOL_PROGRAM_NAME_RE = re.compile(
    r"^(?:.*/)?(?:clang|clang\+\+|clang-cl|clang-\d+(?:\.\d+)*|cc|c\+\+|cc1|gcc|g\+\+|gcc-\d+|"
    r"collect2|ld|ld\.[A-Za-z0-9_]+|lld|lld-link|ar|llvm-[A-Za-z0-9_-]+)$"
)
_SOURCE_FILE_SUFFIX_RE = re.compile(r"\.(?:c|h|i|cc|cpp|cxx|hpp|hh|inc|s|S)$")

COMPILER_CRASH_MARKERS = (
    "PLEASE submit a bug report",
    "frontend command failed due to signal",
    "unable to execute command",
    "Stack dump:",
    "clang frontend command failed with exit code",
)


def _base_dir_spellings(base_dir: Path) -> list[str]:
    spellings = dict.fromkeys([os.path.abspath(base_dir), os.path.realpath(base_dir)])
    return [posixpath.normpath(spelling) for spelling in spellings]


def relative_to_base(path: str, base_dir: Path | None) -> str:
    """``path`` relative to ``base_dir`` (POSIX form) when it lies inside it, otherwise normalised."""
    candidate = path.strip()
    if not candidate:
        return candidate
    normalised = posixpath.normpath(candidate)
    if not os.path.isabs(candidate) or base_dir is None:
        return normalised
    for base in _base_dir_spellings(base_dir):
        if normalised == base:
            return "."
        prefix = base.rstrip("/") + "/"
        if normalised.startswith(prefix):
            return normalised[len(prefix) :]
    return normalised


def _is_option_item(item: str) -> bool:
    return item.startswith(("-W", "-R", "-f")) or bool(_ANALYZER_CHECKER_NAME_RE.match(item))


def _rule_from_option_item(item: str) -> str | None:
    if item == WERROR_FLAG:
        return None
    if item.startswith(WERROR_PREFIX):
        return "-W" + item[len(WERROR_PREFIX) :]
    if item.startswith(("-W", "-R")) or _ANALYZER_CHECKER_NAME_RE.match(item):
        return item
    return None


def split_option_group(message: str) -> tuple[str, str | None]:
    """Split a trailing ``[-Wflag]`` / ``[-Werror,-Wflag]`` / ``[checker.Name]`` group off a message."""
    match = _TRAILING_OPTION_GROUP_RE.match(message)
    if not match:
        return message, None
    items = [item.strip() for item in match["opt"].split(",") if item.strip()]
    if not any(_is_option_item(item) for item in items):
        return message, None
    rule = next((rule for rule in map(_rule_from_option_item, items) if rule is not None), None)
    return match["msg"].rstrip(), rule


@dataclass(frozen=True)
class _CompilerLine:
    level: str
    message: str
    location: Location | None
    from_tool_program: bool


def _parse_compiler_line(line: str, base_dir: Path | None) -> _CompilerLine | None:
    if match := _LOCATED_DIAGNOSTIC_RE.match(line):
        location = Location(
            file=relative_to_base(match["file"], base_dir),
            line=int(match["line"]),
            column=int(match["col"]) if match["col"] else None,
        )
        return _CompilerLine(match["sev"], match["msg"], location, from_tool_program=False)
    if match := _LINELESS_DIAGNOSTIC_RE.match(line):
        prefix = match["file"]
        if _TOOL_PROGRAM_NAME_RE.match(prefix):
            return _CompilerLine(match["sev"], match["msg"], None, from_tool_program=True)
        location = Location(file=relative_to_base(prefix, base_dir))
        return _CompilerLine(match["sev"], match["msg"], location, from_tool_program=False)
    if match := _UNPREFIXED_DIAGNOSTIC_RE.match(line):
        return _CompilerLine(match["sev"], match["msg"], None, from_tool_program=False)
    return None


def _may_be_linker_output(line: str) -> bool:
    return not _LOCATED_DIAGNOSTIC_RE.match(line)


def _continues_previous_diagnostic(line: str, stripped: str) -> bool:
    return line[:1].isspace() or stripped.startswith("|")


def _starts_or_continues_include_stack(line: str, stripped: str, stack_open: bool) -> bool:
    return bool(_INCLUDE_STACK_HEAD_RE.match(stripped) or (stack_open and _INCLUDE_STACK_CONTINUATION_RE.match(line)))


def _linker_reference_location(where: str, function: str | None, base_dir: Path | None) -> Location | None:
    location = Location(function=function)
    section = _LINKER_SECTION_LOCATION_RE.match(where)
    if section and _SOURCE_FILE_SUFFIX_RE.search(section["file"]):
        location = Location(
            file=relative_to_base(section["file"], base_dir),
            line=int(section["line"]) if section["line"] else None,
            function=function,
        )
    return location if (location.file or location.function) else None


@dataclass
class _DiagnosticCollector:
    tool: str
    base_dir: Path | None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    current: Diagnostic | None = None
    pending_context: list[str] = field(default_factory=list)
    linker_function: str | None = None

    def emit(self, diagnostic: Diagnostic) -> None:
        if self.pending_context:
            diagnostic.raw = "\n".join([*self.pending_context, diagnostic.raw or ""])
            self.pending_context.clear()
        self.diagnostics.append(diagnostic)
        self.current = diagnostic

    def attach_note(self, location: Location | None, message: str, line: str) -> None:
        text = "\n".join([*self.pending_context, line])
        self.pending_context.clear()
        if self.current is None:
            orphan = Diagnostic(tool=self.tool, rule_id=None, severity="note", message=message, location=location, raw=text)
            self.diagnostics.append(orphan)
            return
        self.current.path.append(PathStep(location=location, message=message))
        self.current.raw = f"{self.current.raw}\n{text}" if self.current.raw else text

    def feed(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        if _DIAGNOSTIC_COUNT_SUMMARY_RE.match(stripped):
            self.current = None
            return
        if _starts_or_continues_include_stack(line, stripped, bool(self.pending_context)):
            self.pending_context.append(line)
            return
        if _may_be_linker_output(line) and self._feed_linker_line(line, stripped):
            return
        self._feed_compiler_line(line, stripped)

    def _feed_linker_line(self, line: str, stripped: str) -> bool:
        if match := _LINKER_FUNCTION_CONTEXT_RE.match(line):
            self.linker_function = match["func"]
            self.pending_context.append(line)
            return True
        if match := _LINKER_SYMBOL_REFERENCE_RE.match(line):
            location = _linker_reference_location(match["where"], self.linker_function, self.base_dir)
            self.emit(
                Diagnostic(
                    tool=self.tool,
                    rule_id=LINKER_ERROR_RULE,
                    severity="error",
                    message=match["msg"],
                    location=location,
                    raw=line,
                )
            )
            self.linker_function = None
            return True
        if match := _LINKER_MESSAGE_RE.match(line):
            is_warning = match["sev"] == "warning"
            self.emit(
                Diagnostic(
                    tool=self.tool,
                    rule_id=LINKER_WARNING_RULE if is_warning else LINKER_ERROR_RULE,
                    severity="warning" if is_warning else "error",
                    message=match["msg"],
                    raw=line,
                )
            )
            return True
        if match := _LLD_REFERENCE_DETAIL_RE.match(stripped):
            self.attach_note(None, match["msg"], line)
            return True
        return False

    def _feed_compiler_line(self, line: str, stripped: str) -> None:
        parsed = _parse_compiler_line(line, self.base_dir)
        if parsed is None:
            if self.current is not None and _continues_previous_diagnostic(line, stripped):
                self.current.raw = f"{self.current.raw}\n{line}" if self.current.raw else line
            return
        message, rule = split_option_group(parsed.message)
        if parsed.level == "note":
            self.attach_note(parsed.location, message, line)
            return
        if rule is None:
            rule = _DEFAULT_RULE_BY_LEVEL[parsed.level]
            if parsed.from_tool_program and "linker command failed" in message:
                rule = LINKER_ERROR_RULE
        severity = _SEVERITY_BY_LEVEL[parsed.level]
        self.emit(Diagnostic(tool=self.tool, rule_id=rule, severity=severity, message=message, location=parsed.location, raw=line))


def parse_clang_diagnostics(text: str, tool: str = "clang", *, base_dir: Path | None = None) -> list[Diagnostic]:
    """Parse clang and linker output; ``note:`` lines become path steps of the preceding diagnostic."""
    collector = _DiagnosticCollector(tool=tool, base_dir=base_dir)
    for raw_line in (text or "").splitlines():
        collector.feed(raw_line.rstrip("\r\n"))
    return collector.diagnostics


def crash_signal(signal: int | None, exit_code: int | None) -> int | None:
    if signal is not None:
        return signal
    return signal_from_exit_status(exit_code)


def _has_crash_marker_outside_source_diagnostics(stderr: str) -> bool:
    return any(
        any(marker in line for marker in COMPILER_CRASH_MARKERS)
        for line in (stderr or "").splitlines()
        if not _LOCATED_DIAGNOSTIC_RE.match(line)
    )


def looks_like_crash(proc_stderr: str, signal: int | None, exit_code: int | None = None) -> bool:
    """Whether clang itself crashed, as opposed to rejecting the input."""
    if crash_signal(signal, exit_code) is not None:
        return True
    return _has_crash_marker_outside_source_diagnostics(proc_stderr)


def crash_reason(proc_stderr: str, signal: int | None, exit_code: int | None = None) -> str:
    number = crash_signal(signal, exit_code)
    return f"killed by signal {number}" if number is not None else "crash reported on stderr"
