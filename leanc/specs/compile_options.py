from __future__ import annotations

SANITIZER_FLAG_PREFIXES = ("-fsanitize", "-fno-sanitize")
TOOL_PASS_THROUGH_PREFIXES = ("-X", "-mllvm", "-Wp,", "-Wa,", "-Wl,")
ANALYZER_MODE_PREFIXES = ("--analyze",)
INDIRECTION_PREFIXES = ("@", "--config")
CODE_LOADING_PREFIXES = ("-fplugin", "-fpass-plugin")
TOOLCHAIN_REPLACEMENT_PREFIXES = (
    "-B",
    "-ccc-",
    "-resource-dir",
    "--resource-dir",
    "--sysroot",
    "-isysroot",
    "--gcc-toolchain",
    "--gcc-install-dir",
    "-nostdlib",
    "-nodefaultlibs",
    "-nostartfiles",
)
TOOL_CONTROL_PREFIXES = (
    *TOOL_PASS_THROUGH_PREFIXES,
    *ANALYZER_MODE_PREFIXES,
    *INDIRECTION_PREFIXES,
    *CODE_LOADING_PREFIXES,
    *TOOLCHAIN_REPLACEMENT_PREFIXES,
)
SANITIZER_CONTROL_MESSAGE = "sanitizers are chosen by tests.sanitizers, not compile.flags"
TOOL_CONTROL_MESSAGE = (
    "compile.flags must not pass options to an internal tool (frontend, analyzer, LLVM, preprocessor, assembler, "
    "linker), read options from a file, load plugins or replace the toolchain"
)

WARNING_OPTION_PREFIX = "-W"
DIAGNOSTIC_ONLY_FLAGS = frozenset({"-w", "--no-warnings", "-pedantic", "-pedantic-errors"})
DEBUG_INFO_PREFIX = "-g"


def sanitizer_control_flags(flags: list[str]) -> list[str]:
    return [flag for flag in flags if flag.startswith(SANITIZER_FLAG_PREFIXES)]


def tool_control_flags(flags: list[str]) -> list[str]:
    return [flag for flag in flags if flag.startswith(TOOL_CONTROL_PREFIXES)]


def is_diagnostic_option(flag: str) -> bool:
    return flag in DIAGNOSTIC_ONLY_FLAGS or flag.startswith(WARNING_OPTION_PREFIX)


def is_debug_info_option(flag: str) -> bool:
    return flag.startswith(DEBUG_INFO_PREFIX)


def has_no_semantic_effect(flag: str) -> bool:
    return is_diagnostic_option(flag) or is_debug_info_option(flag)


def semantic_flags(flags: list[str]) -> list[str]:
    return [flag for flag in flags if not has_no_semantic_effect(flag)]
