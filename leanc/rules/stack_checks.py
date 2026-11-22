from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

from leanc.foundation.models import PolicyViolation
from leanc.rules.findings import violation
from leanc.specs.compile_options import is_diagnostic_option

__all__ = [
    "MAX_STACK_FRAME_BYTES",
    "STACK_FRAME_CHECK_FLAGS",
    "STACK_FRAME_RULE",
    "SYSTEM_HEADER_WARNINGS_FLAG",
    "UNBOUNDED_STACK_CHECK_FLAGS",
    "UNBOUNDED_STACK_RULE",
    "StackUsage",
    "UnboundedStackScan",
    "codegen_flags",
    "stack_usage_findings",
    "stack_usage_path",
    "unbounded_stack_scan",
]

MAX_STACK_FRAME_BYTES = 1048576
UNBOUNDED_STACK_RULE = "UNBOUNDED_STACK"
STACK_FRAME_RULE = "STACK_FRAME_TOO_LARGE"
STACK_FRAME_CONSEQUENCE = (
    "a stack frame this large overflows the native stack, while CBMC has no stack limit and only runs out of memory"
)
STACK_USAGE_SUFFIX = ".su"
STATIC_FRAME_QUALIFIER = "static"
RUNTIME_STACK_CONSEQUENCE = "CBMC has no model of stack limits, so an input-sized stack allocation verifies"

DIAGNOSTIC_LAYOUT_FLAGS: tuple[str, ...] = (
    "-fno-color-diagnostics",
    "-fno-caret-diagnostics",
    "-fdiagnostics-show-option",
    "-fshow-column",
    "-fshow-source-location",
    "-fdiagnostics-format=clang",
    "-fmessage-length=0",
)
UNBOUNDED_STACK_CHECK_FLAGS: tuple[str, ...] = (
    "-fsyntax-only",
    *DIAGNOSTIC_LAYOUT_FLAGS,
    "-Wno-everything",
    "-Wvla",
    "-Walloca",
    "-Werror=vla",
    "-Werror=alloca",
)
SYSTEM_HEADER_WARNINGS_FLAG = "-Wsystem-headers"
STACK_FRAME_CHECK_FLAGS: tuple[str, ...] = (
    "-O0",
    "-fno-lto",
    "-fembed-bitcode=off",
    "-fstack-usage",
    "-Wno-everything",
    "-S",
)

_STACK_DIAG_RE = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):\d+:(?:(?:\{[^}\n]*\})+:)? (?:error|warning): (?P<msg>.*?)"
    r"\[-W(?:error,)?-?W?(?:vla|alloca)[^\]]*\]",
    re.MULTILINE,
)
_ERROR_LINE_RE = re.compile(r": (?:fatal )?error: ")
_STACK_USAGE_ROW_RE = re.compile(
    r"^(?P<file>.+?)(?::(?P<line>\d+))?:(?P<function>[^\t:]+)\t(?P<size>\d+)\t(?P<qualifier>\S+)$"
)


@dataclass(frozen=True)
class UnboundedStackScan:
    violations: list[PolicyViolation] = field(default_factory=list)
    outside_snapshot: list[str] = field(default_factory=list)
    unparsed_errors: list[str] = field(default_factory=list)

    @property
    def accounts_for_failure(self) -> bool:
        return bool(self.violations or self.outside_snapshot) and not self.unparsed_errors


@dataclass(frozen=True)
class StackUsage:
    violations: list[PolicyViolation] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)


def codegen_flags(flags: list[str]) -> list[str]:
    return [flag for flag in flags if not is_diagnostic_option(flag)]


def stack_usage_path(assembly: Path) -> Path:
    return assembly.with_suffix(STACK_USAGE_SUFFIX)


def _line_in(rel: str, file: str, line: str | None) -> int | None:
    same_file = posixpath.normpath(file) == posixpath.normpath(rel)
    return int(line) if line is not None and same_file else None


def unbounded_stack_scan(stderr: str, reason: str, *, project_files_only: bool) -> UnboundedStackScan:
    scan = UnboundedStackScan()
    for found in _STACK_DIAG_RE.finditer(stderr):
        file = posixpath.normpath(found.group("file"))
        if project_files_only and posixpath.isabs(file):
            scan.outside_snapshot.append(found.group())
        else:
            scan.violations.append(
                violation(UNBOUNDED_STACK_RULE, f"{found.group('msg').strip()} ({reason})", file, int(found.group("line")))
            )
    scan.unparsed_errors.extend(
        line for line in stderr.splitlines() if _ERROR_LINE_RE.search(line) and not _STACK_DIAG_RE.match(line)
    )
    return scan


def _frame_message(function: str, size: int) -> str:
    return (
        f"function '{function}' has a {size}-byte stack frame, more than the {MAX_STACK_FRAME_BYTES}-byte "
        f"limit ({STACK_FRAME_CONSEQUENCE})"
    )


def stack_usage_findings(rel: str, text: str) -> StackUsage:
    usage = StackUsage()
    for row in text.splitlines():
        found = _STACK_USAGE_ROW_RE.match(row)
        if found is None:
            usage.unparsed.append(row)
        else:
            usage.violations.extend(_frame_violations(rel, found))
    return usage


def _frame_violations(rel: str, row: re.Match[str]) -> list[PolicyViolation]:
    function, size = row.group("function"), int(row.group("size"))
    line = _line_in(rel, row.group("file"), row.group("line"))
    found = []
    if row.group("qualifier") != STATIC_FRAME_QUALIFIER:
        found.append(violation(UNBOUNDED_STACK_RULE, _runtime_stack_message(function, row.group("qualifier")), rel,
                               line))
    if size > MAX_STACK_FRAME_BYTES:
        found.append(violation(STACK_FRAME_RULE, _frame_message(function, size), rel, line))
    return found


def _runtime_stack_message(function: str, qualifier: str) -> str:
    return (
        f"function '{function}' allocates stack space at run time (clang -fstack-usage reports '{qualifier}', "
        f"not '{STATIC_FRAME_QUALIFIER}'; {RUNTIME_STACK_CONSEQUENCE})"
    )
