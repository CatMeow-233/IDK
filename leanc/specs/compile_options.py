from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

SANITIZER_FLAG_PREFIXES = ("-fsanitize", "-fno-sanitize")
TOOL_PASS_THROUGH_PREFIXES = ("-X", "-mllvm", "-Wp,", "-Wa,", "-Wl,", "--for-linker")
ANALYZER_MODE_PREFIXES = ("--analyze",)
INDIRECTION_PREFIXES = ("@", "--config")
CODE_LOADING_PREFIXES = ("-fplugin", "-fpass-plugin")
DRIVER_MODE_PREFIXES = ("--driver-mode",)
FILE_SYSTEM_OVERLAY_PREFIXES = ("-ivfsoverlay", "-vfsoverlay", "--vfsoverlay")
TOOLCHAIN_REPLACEMENT_PREFIXES = (
    "-B",
    "--prefix",
    "--ld-path",
    "-ccc-",
    "-resource-dir",
    "--resource-dir",
    "--sysroot",
    "-isysroot",
    "--gcc-toolchain",
    "--gcc-install-dir",
    "-nostdlib",
    "--no-standard-libraries",
    "-nodefaultlibs",
    "-nostartfiles",
)
LINKER_SELECTION_PREFIX = "-fuse-ld="
PATH_SEPARATOR = "/"
TOOL_CONTROL_PREFIXES = (
    *TOOL_PASS_THROUGH_PREFIXES,
    *ANALYZER_MODE_PREFIXES,
    *INDIRECTION_PREFIXES,
    *CODE_LOADING_PREFIXES,
    *DRIVER_MODE_PREFIXES,
    *FILE_SYSTEM_OVERLAY_PREFIXES,
    *TOOLCHAIN_REPLACEMENT_PREFIXES,
)
PREPROCESSOR_OUTPUT_FLAGS = frozenset({
    "-E",
    "--preprocess",
    "-P",
    "--no-line-commands",
    "-C",
    "--comments",
    "-CC",
    "--comments-in-macros",
    "-M",
    "--dependencies",
    "-MM",
    "--user-dependencies",
    "-traditional-cpp",
    "--traditional-cpp",
    "-fdirectives-only",
    "-frewrite-includes",
    "-frewrite-imports",
    "-fkeep-system-includes",
    "-fminimize-whitespace",
    "-fuse-line-directives",
    "-fsyntax-only",
    "-fdriver-only",
})
PREPROCESSOR_DUMP_PREFIX = "-d"
CONST_STRING_FLAG = "-Wwrite-strings"
DIGRAPH_CONTROL_FLAGS = frozenset({"-fdigraphs", "-fno-digraphs"})
STRICT_C89_ALIASES = frozenset({"-ansi", "--ansi"})
STRICT_C89_STANDARDS = frozenset({"c89", "c90", "iso9899:1990", "iso9899:199409"})
C23_STANDARDS = frozenset({"c23", "c2x", "gnu23", "gnu2x"})
TOKENISATION_CHANGING_STANDARDS = STRICT_C89_STANDARDS | C23_STANDARDS
STANDARD_OPTION_PREFIXES = ("-std=", "--std=")
OPTION_VALUE_SEPARATOR = "="
MACRO_DEFINING_WARNING_FLAGS = frozenset({"-Wdeprecated", "-Wno-deprecated"})
OPTION_PREFIX = "-"
NON_OPTION_ENTRIES = frozenset({"-", "--"})
SANITIZER_CONTROL_MESSAGE = "sanitizers are chosen by tests.sanitizers, not compile.flags"
TOOL_CONTROL_MESSAGE = (
    "compile.flags must not pass options to an internal tool (frontend, analyzer, LLVM, preprocessor, assembler, "
    "linker), read options from a file, load plugins, switch the driver mode, overlay the file system or replace the "
    "toolchain"
)
PREPROCESSOR_OUTPUT_MESSAGE = (
    "compile.flags must not change what the preprocessor prints or stop before code generation: integrity reads "
    "the preprocessed source with these flags"
)
LEXICAL_MESSAGE = (
    "compile.flags must not change how C source is split into tokens: strict C89/C90 (-std=c89, -std=c90, "
    "-std=iso9899:1990, -std=iso9899:199409, -ansi) has no // comments, C23 (-std=c23, -std=c2x, -std=gnu23, "
    "-std=gnu2x) has digit separators and -fdigraphs / -fno-digraphs switch digraphs, while CBMC and Frama-C "
    "preprocess in gcc's gnu modes up to gnu11"
)
CONST_STRING_MESSAGE = (
    f"{CONST_STRING_FLAG} gives C string literals the type const char[N] (clang passes -fconst-strings to the "
    "frontend), which CBMC, Frama-C and the stack checks do not apply"
)
NON_OPTION_MESSAGE = (
    "every compile.flags entry must be one option with its value joined to it (-I<dir>, -D<name>, -std=<standard>); "
    "input files, '-' and '--' are not options"
)

WARNING_OPTION_PREFIX = "-W"
DIAGNOSTIC_ONLY_FLAGS = frozenset({"-w", "--no-warnings", "-pedantic", "-pedantic-errors"})
DEBUG_INFO_PREFIX = "-g"


def sanitizer_control_flags(flags: list[str]) -> list[str]:
    return [flag for flag in flags if flag.startswith(SANITIZER_FLAG_PREFIXES)]


def non_option_entries(flags: list[str]) -> list[str]:
    return [flag for flag in flags if not flag.startswith(OPTION_PREFIX) or flag in NON_OPTION_ENTRIES]


def preprocessor_output_flags(flags: list[str]) -> list[str]:
    return [flag for flag in flags if flag in PREPROCESSOR_OUTPUT_FLAGS or flag.startswith(PREPROCESSOR_DUMP_PREFIX)]


def _selects_tokenisation_changing_standard(flag: str) -> bool:
    if flag in STRICT_C89_ALIASES:
        return True
    standard = flag.partition(OPTION_VALUE_SEPARATOR)[2]
    return flag.startswith(STANDARD_OPTION_PREFIXES) and standard in TOKENISATION_CHANGING_STANDARDS


def lexical_flags(flags: list[str]) -> list[str]:
    return [flag for flag in flags if flag in DIGRAPH_CONTROL_FLAGS or _selects_tokenisation_changing_standard(flag)]


def const_string_flags(flags: list[str]) -> list[str]:
    return [flag for flag in flags if flag == CONST_STRING_FLAG]


def selects_linker_by_path(flag: str) -> bool:
    return flag.startswith(LINKER_SELECTION_PREFIX) and PATH_SEPARATOR in flag


def tool_control_flags(flags: list[str]) -> list[str]:
    return [flag for flag in flags if flag.startswith(TOOL_CONTROL_PREFIXES) or selects_linker_by_path(flag)]


@dataclass(frozen=True)
class FlagRejection:
    message: str
    flags: list[str]


FlagSelector = Callable[[list[str]], list[str]]
REJECTED_FLAG_FAMILIES: tuple[tuple[str, FlagSelector], ...] = (
    (SANITIZER_CONTROL_MESSAGE, sanitizer_control_flags),
    (TOOL_CONTROL_MESSAGE, tool_control_flags),
    (NON_OPTION_MESSAGE, non_option_entries),
    (PREPROCESSOR_OUTPUT_MESSAGE, preprocessor_output_flags),
    (LEXICAL_MESSAGE, lexical_flags),
    (CONST_STRING_MESSAGE, const_string_flags),
)


def flag_rejection(flags: list[str]) -> FlagRejection | None:
    for message, select in REJECTED_FLAG_FAMILIES:
        selected = select(flags)
        if selected:
            return FlagRejection(message, selected)
    return None


def is_diagnostic_option(flag: str) -> bool:
    if flag == CONST_STRING_FLAG or flag in MACRO_DEFINING_WARNING_FLAGS:
        return False
    return flag in DIAGNOSTIC_ONLY_FLAGS or flag.startswith(WARNING_OPTION_PREFIX)


def is_debug_info_option(flag: str) -> bool:
    return flag.startswith(DEBUG_INFO_PREFIX)


def has_no_semantic_effect(flag: str) -> bool:
    return is_diagnostic_option(flag) or is_debug_info_option(flag)


class CharSignedness(Enum):
    SIGNED = "signed"
    UNSIGNED = "unsigned"


CHAR_SIGNEDNESS_FLAGS: dict[str, CharSignedness] = {
    "-fsigned-char": CharSignedness.SIGNED,
    "-fno-unsigned-char": CharSignedness.SIGNED,
    "--signed-char": CharSignedness.SIGNED,
    "-funsigned-char": CharSignedness.UNSIGNED,
    "-fno-signed-char": CharSignedness.UNSIGNED,
    "--unsigned-char": CharSignedness.UNSIGNED,
}


def is_char_signedness_option(flag: str) -> bool:
    return flag in CHAR_SIGNEDNESS_FLAGS


def requested_char_signedness(flags: list[str]) -> CharSignedness | None:
    requested = None
    for flag in flags:
        requested = CHAR_SIGNEDNESS_FLAGS.get(flag, requested)
    return requested


def is_standard_option(flag: str) -> bool:
    return flag.startswith(STANDARD_OPTION_PREFIXES)


def option_value(flag: str) -> str:
    return flag.partition(OPTION_VALUE_SEPARATOR)[2]


def requested_standard(flags: list[str]) -> str | None:
    standards = [option_value(flag) for flag in flags if is_standard_option(flag)]
    return standards[-1] if standards else None
