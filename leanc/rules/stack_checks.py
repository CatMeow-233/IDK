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
    "UNBOUNDED_STACK_CHECK_FLAGS",
    "UNBOUNDED_STACK_RULE",
    "StackUsage",
    "codegen_flags",
    "stack_usage_findings",
    "stack_usage_path",
    "unbounded_stack_violations",
]

MAX_STACK_FRAME_BYTES = 1048576
UNBOUNDED_STACK_RULE = "UNBOUNDED_STACK"
STACK_FRAME_RULE = "STACK_FRAME_TOO_LARGE"
STACK_FRAME_CONSEQUENCE = (
    "a stack frame this large overflows the native stack, while CBMC has no stack limit and only runs out of memory"
)
STACK_USAGE_SUFFIX = ".su"

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
STACK_FRAME_CHECK_FLAGS: tuple[str, ...] = ("-O0", "-fno-lto", "-fstack-usage", "-Wno-everything", "-S")

_STACK_DIAG_RE = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):\d+:(?:(?:\{[^}\n]*\})+:)? (?:error|warning): (?P<msg>.*?)"
    r"\[-W(?:error,)?-?W?(?:vla|alloca)[^\]]*\]",
    re.MULTILINE,
)
_STACK_USAGE_ROW_RE = re.compile(
    r"^(?P<file>.+?)(?::(?P<line>\d+))?:(?P<function>[^\t:]+)\t(?P<size>\d+)\t\S+$"
)


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


def unbounded_stack_violations(rel: str, stderr: str, reason: str) -> list[PolicyViolation]:
    return [
        violation(UNBOUNDED_STACK_RULE, f"{found.group('msg').strip()} ({reason})", rel, int(found.group("line")))
        for found in _STACK_DIAG_RE.finditer(stderr)
    ]


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
        elif int(found.group("size")) > MAX_STACK_FRAME_BYTES:
            usage.violations.append(
                violation(STACK_FRAME_RULE, _frame_message(found.group("function"), int(found.group("size"))), rel,
                          _line_in(rel, found.group("file"), found.group("line")))
            )
    return usage
