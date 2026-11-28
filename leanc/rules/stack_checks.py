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
    "IncludeSplit",
    "StackUsage",
    "UnboundedStackScan",
    "codegen_flags",
    "project_relative",
    "stack_usage_findings",
    "stack_usage_path",
    "system_includes_as_user_includes",
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
USER_INCLUDE_FLAG = "-I"
USER_FRAMEWORK_FLAG = "-F"
SYSTEM_INCLUDE_REWRITES: tuple[tuple[str, str], ...] = (
    ("-isystem-after", USER_INCLUDE_FLAG),
    ("-isystem", USER_INCLUDE_FLAG),
    ("-idirafter", USER_INCLUDE_FLAG),
    ("-iwithsysroot", USER_INCLUDE_FLAG),
    ("-cxx-isystem", USER_INCLUDE_FLAG),
    ("-stdlib++-isystem", USER_INCLUDE_FLAG),
    ("--include-directory-after=", USER_INCLUDE_FLAG),
    ("-iframeworkwithsysroot", USER_FRAMEWORK_FLAG),
    ("-iframework", USER_FRAMEWORK_FLAG),
)
USER_PREFIXED_INCLUDE_SPELLINGS = ("-iwithprefixbefore", "--include-with-prefix-before=")
INCLUDE_PREFIX_SPELLINGS = ("-iprefix", "--include-prefix=")
PREFIXED_SYSTEM_INCLUDE_SPELLINGS = ("-iwithprefix", "--include-with-prefix-after=", "--include-with-prefix=")
SYSTEM_HEADER_PREFIX_FLAG = "--system-header-prefix="
STACK_FRAME_CHECK_FLAGS: tuple[str, ...] = (
    "-O0",
    "-fno-lto",
    "-fembed-bitcode=off",
    "-fstack-usage",
    "-Wno-everything",
    "-S",
)

_STACK_DIAG_RE = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[^:\n]+):(?P<line>\d+):\d+:(?:(?:\{[^}\n]*\})+:)? (?:error|warning): (?P<msg>.*?)"
    r"\[-W(?:error,)?-?W?(?:vla|alloca)[^\]]*\]",
    re.MULTILINE,
)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:/")
_ERROR_LINE_RE = re.compile(r"(?:^|: )(?:fatal )?error: ")
_STACK_USAGE_ROW_RE = re.compile(
    r"^(?P<file>.+?)(?::(?P<line>\d+))?:(?P<function>[^\t:]+)\t(?P<size>\d+)\t(?P<qualifier>\S+)$"
)


@dataclass(frozen=True)
class UnboundedStackScan:
    violations: list[PolicyViolation] = field(default_factory=list)
    unparsed_errors: list[str] = field(default_factory=list)

    @property
    def accounts_for_failure(self) -> bool:
        return bool(self.violations) and not self.unparsed_errors


@dataclass(frozen=True)
class StackUsage:
    violations: list[PolicyViolation] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)


def codegen_flags(flags: list[str]) -> list[str]:
    return [flag for flag in flags if not is_diagnostic_option(flag)]


def stack_usage_path(assembly: Path) -> Path:
    return assembly.with_suffix(STACK_USAGE_SUFFIX)


def _posix_slashes(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/"))


def _is_absolute_posix_or_drive(path: str) -> bool:
    return posixpath.isabs(path) or bool(_WINDOWS_DRIVE_RE.match(path))


def _line_in(rel: str, file: str, line: str | None) -> int | None:
    same_file = _posix_slashes(file) == _posix_slashes(rel)
    return int(line) if line is not None and same_file else None


def project_relative(file: str, project_root: str) -> str | None:
    normalized = _posix_slashes(file)
    if not _is_absolute_posix_or_drive(normalized):
        return normalized
    root = _posix_slashes(project_root)
    if _WINDOWS_DRIVE_RE.match(normalized) and _WINDOWS_DRIVE_RE.match(root):
        if normalized[:2].lower() != root[:2].lower():
            return None
        prefix = root.rstrip("/") + "/"
        if normalized.lower().startswith(prefix.lower()):
            return normalized[len(prefix) :]
        return "." if normalized.lower() == root.lower() else None
    if posixpath.isabs(normalized) and posixpath.isabs(root):
        if posixpath.commonpath([normalized, root]) == root:
            return posixpath.relpath(normalized, root)
    return None


def _option_value(flag: str, spellings: tuple[str, ...]) -> str | None:
    for spelling in spellings:
        if flag.startswith(spelling):
            return flag[len(spelling) :]
    return None


def _as_user_include(flag: str, include_prefix: str) -> str | None:
    if flag.startswith(USER_PREFIXED_INCLUDE_SPELLINGS):
        return None
    prefixed = _option_value(flag, PREFIXED_SYSTEM_INCLUDE_SPELLINGS)
    if prefixed is not None:
        return USER_INCLUDE_FLAG + include_prefix + prefixed
    for prefix, user_flag in SYSTEM_INCLUDE_REWRITES:
        if flag.startswith(prefix):
            return user_flag + flag[len(prefix) :]
    return None


@dataclass(frozen=True)
class IncludeSplit:
    kept: list[str]
    system_as_user: list[str]


def system_includes_as_user_includes(flags: list[str]) -> IncludeSplit:
    split = IncludeSplit(kept=[], system_as_user=[])
    include_prefix = ""
    for flag in flags:
        include_prefix = _option_value(flag, INCLUDE_PREFIX_SPELLINGS) or include_prefix
        rewritten = _as_user_include(flag, include_prefix)
        if rewritten is not None:
            split.system_as_user.append(rewritten)
        elif not flag.startswith(SYSTEM_HEADER_PREFIX_FLAG):
            split.kept.append(flag)
    return split


def unbounded_stack_scan(stderr: str, reason: str, *, project_root: str) -> UnboundedStackScan:
    scan = UnboundedStackScan()
    for found in _STACK_DIAG_RE.finditer(stderr):
        relative = project_relative(found.group("file"), project_root)
        file = relative if relative is not None else posixpath.normpath(found.group("file"))
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
