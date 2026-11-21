from __future__ import annotations

import os
import posixpath
import re

from leanc.foundation.models import PolicyViolation
from leanc.rules.findings import violation

__all__ = [
    "MAX_STACK_FRAME_BYTES",
    "STACK_FRAME_CHECK_FLAGS",
    "STACK_FRAME_RULE",
    "UNBOUNDED_STACK_CHECK_FLAGS",
    "UNBOUNDED_STACK_RULE",
    "codegen_flags",
    "stack_frame_violations",
    "unbounded_stack_violations",
]

MAX_STACK_FRAME_BYTES = 1048576
UNBOUNDED_STACK_RULE = "UNBOUNDED_STACK"
STACK_FRAME_RULE = "STACK_FRAME_TOO_LARGE"
STACK_FRAME_CONSEQUENCE = (
    "a stack frame this large overflows the native stack, while CBMC has no stack limit and only runs out of memory"
)
UNKNOWN_FUNCTION = "?"
UNKNOWN_SIZE = "?"

UNBOUNDED_STACK_CHECK_FLAGS: tuple[str, ...] = (
    "-fsyntax-only",
    "-fno-color-diagnostics",
    "-fno-caret-diagnostics",
    "-Wno-everything",
    "-Wvla",
    "-Walloca",
    "-Werror=vla",
    "-Werror=alloca",
)
STACK_FRAME_CHECK_FLAGS: tuple[str, ...] = (
    "-O0",
    "-fno-color-diagnostics",
    "-fno-caret-diagnostics",
    "-fdiagnostics-show-option",
    "-Wno-everything",
    f"-Wframe-larger-than={MAX_STACK_FRAME_BYTES}",
    "-S",
    "-o",
    os.devnull,
)

WARNING_SILENCING_FLAGS = frozenset({"-w", "--no-warnings"})
WARNING_OPTION_PREFIX = "-W"
PASS_THROUGH_OPTION_PREFIXES = ("-Wl,", "-Wa,", "-Wp,")
FRONTEND_PASS_THROUGH_FLAG = "-Xclang"

_STACK_DIAG_RE = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):\d+: (?:error|warning): (?P<msg>.*?)\[-W(?:error,)?-?W?(?:vla|alloca)[^\]]*\]",
    re.MULTILINE,
)
_FRAME_DIAG_RE = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):\d+: (?:error|warning): (?P<msg>.*?)\s*"
    r"\[-W(?:error,)?-?W?frame-larger-than[^\]]*\]",
    re.MULTILINE,
)
_FRAME_SIZE_RE = re.compile(r"\d+")
_QUOTED_FUNCTION_RE = re.compile(r"'([^'\n]+)'")


def _controls_warnings(flag: str) -> bool:
    if flag in WARNING_SILENCING_FLAGS:
        return True
    return flag.startswith(WARNING_OPTION_PREFIX) and not flag.startswith(PASS_THROUGH_OPTION_PREFIXES)


def codegen_flags(flags: list[str]) -> list[str]:
    kept: list[str] = []
    index = 0
    while index < len(flags):
        flag = flags[index]
        has_forwarded = flag == FRONTEND_PASS_THROUGH_FLAG and index + 1 < len(flags)
        group = flags[index : index + 2] if has_forwarded else [flag]
        if not _controls_warnings(group[-1]):
            kept += group
        index += len(group)
    return kept


def _line_in(rel: str, file: str, line: str) -> int | None:
    return int(line) if posixpath.normpath(file) == posixpath.normpath(rel) else None


def unbounded_stack_violations(rel: str, stderr: str, reason: str) -> list[PolicyViolation]:
    return [
        violation(UNBOUNDED_STACK_RULE, f"{found.group('msg').strip()} ({reason})", rel, int(found.group("line")))
        for found in _STACK_DIAG_RE.finditer(stderr)
    ]


def _frame_message(text: str) -> str:
    size = _FRAME_SIZE_RE.search(text)
    functions = _QUOTED_FUNCTION_RE.findall(text)
    function = functions[-1] if functions else UNKNOWN_FUNCTION
    shown_size = size.group() if size else UNKNOWN_SIZE
    return (
        f"function '{function}' has a {shown_size}-byte stack frame, more than the {MAX_STACK_FRAME_BYTES}-byte "
        f"limit ({STACK_FRAME_CONSEQUENCE})"
    )


def stack_frame_violations(rel: str, stderr: str) -> list[PolicyViolation]:
    return [
        violation(STACK_FRAME_RULE, _frame_message(found.group("msg")), rel,
                  _line_in(rel, found.group("file"), found.group("line")))
        for found in _FRAME_DIAG_RE.finditer(stderr)
    ]
