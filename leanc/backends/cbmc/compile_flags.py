from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from leanc.specs.model import CompileConfig

DEFINE_FLAG = "-D"
UNDEFINE_FLAG = "-U"
INCLUDE_DIR_FLAG = "-I"
STANDARD_FLAG_PREFIX = "-std="
ANSI_FLAG = "-ansi"
ANSI_STANDARD = "c89"
CBMC_UNSIGNED_CHAR_FLAG = "--unsigned-char"
DEFINES_DISABLING_HARNESS_ASSERTIONS = frozenset({"NDEBUG"})
FORWARDED_FLAGS_TAKING_SEPARATE_VALUE = frozenset({DEFINE_FLAG, UNDEFINE_FLAG, INCLUDE_DIR_FLAG})
UNAPPLIED_FLAGS_TAKING_SEPARATE_VALUE = frozenset({"-include", "-imacros", "-isystem", "-iquote", "-idirafter", "-x"})
SINGLE_FLAG_WIDTH = 1
FLAG_WITH_SEPARATE_VALUE_WIDTH = 2

WARNING_FLAG_PREFIX = "-W"
TOOL_PASSTHROUGH_PREFIXES = ("-Wl,", "-Wa,", "-Wp,")
DEBUG_INFO_FLAG_PREFIX = "-g"
DIAGNOSTICS_ONLY_FLAGS = frozenset({"-w", "-pedantic", "-pedantic-errors"})

C89_MODE = "--c89"
C99_MODE = "--c99"
C11_MODE = "--c11"
CBMC_DEFAULT_MODE = C11_MODE
CBMC_PREPROCESSOR_STANDARDS: dict[str, str] = {C89_MODE: "gnu89", C99_MODE: "gnu99", C11_MODE: "gnu11"}
CBMC_PREPROCESSOR_DEFINES: tuple[str, ...] = ("-D__CPROVER__",)
GCC_UNSIGNED_CHAR_FLAG = "-funsigned-char"
C11_REVISION_NOTE = (
    "CBMC has no C17 mode; C17 only resolves defects of C11, but __STDC_VERSION__ is 201112L instead of 201710L"
)


class CharSignedness(Enum):
    SIGNED = "signed"
    UNSIGNED = "unsigned"


CHAR_SIGNEDNESS_FLAGS: dict[str, CharSignedness] = {
    "-fsigned-char": CharSignedness.SIGNED,
    "-fno-unsigned-char": CharSignedness.SIGNED,
    "-funsigned-char": CharSignedness.UNSIGNED,
    "-fno-signed-char": CharSignedness.UNSIGNED,
}
SIGNED_CHAR_REQUEST = "-fsigned-char"
SIGNED_CHAR_ARCHITECTURES = frozenset({"x86_64", "i386", "x32"})
PLATFORM_ARCHITECTURE_FIELD = 1


@dataclass(frozen=True)
class StandardMode:
    flag: str
    approximation: str | None = None


CBMC_STANDARD_MODES: dict[str, StandardMode] = {
    **dict.fromkeys(("c89", "c90", "gnu89", "gnu90", "iso9899:1990", "iso9899:199409"), StandardMode(C89_MODE)),
    **dict.fromkeys(("c99", "c9x", "gnu99", "gnu9x", "iso9899:1999", "iso9899:199x"), StandardMode(C99_MODE)),
    **dict.fromkeys(("c11", "c1x", "gnu11", "gnu1x", "iso9899:2011"), StandardMode(C11_MODE)),
    **dict.fromkeys(
        ("c17", "c18", "gnu17", "gnu18", "iso9899:2017", "iso9899:2018"), StandardMode(C11_MODE, C11_REVISION_NOTE)
    ),
}


@dataclass(frozen=True)
class CompileFlagTranslation:
    macro_args: tuple[str, ...]
    include_args: tuple[str, ...]
    semantic_args: tuple[str, ...]
    dropped_defines: tuple[str, ...]
    not_applied: tuple[str, ...]
    no_semantic_effect: tuple[str, ...]
    standard: str
    standard_mode: StandardMode | None
    char_signedness: CharSignedness | None

    def not_applied_on(self, architecture: str | None) -> list[str]:
        unapplied = list(self.not_applied)
        if self.char_signedness is CharSignedness.SIGNED and architecture not in SIGNED_CHAR_ARCHITECTURES:
            unapplied.append(SIGNED_CHAR_REQUEST)
        return unapplied

    def details(self) -> dict:
        return {
            "forwarded": [*self.include_args, *self.macro_args, *self.semantic_args],
            "dropped_defines": list(self.dropped_defines),
            "not_applied": list(self.not_applied),
            "no_semantic_effect": list(self.no_semantic_effect),
            "standard": self.standard,
            "standard_mode": self.standard_mode.flag if self.standard_mode else None,
            "char_signedness": self.char_signedness.value if self.char_signedness else None,
        }


@dataclass
class _MacroOperations:
    forwarded: list[tuple[str, str]] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)

    def define(self, arg: str, spelling: str) -> None:
        name = macro_name(arg)
        if name in DEFINES_DISABLING_HARNESS_ASSERTIONS:
            self.dropped.append((name, spelling))
        else:
            self.forwarded.append((name, arg))

    def undefine(self, name: str) -> bool:
        defined_earlier = any(defined == name for defined, _ in [*self.forwarded, *self.dropped])
        self.forwarded = [entry for entry in self.forwarded if entry[0] != name]
        self.dropped = [entry for entry in self.dropped if entry[0] != name]
        return defined_earlier


@dataclass
class _Translator:
    standard: str
    macros: _MacroOperations = field(default_factory=_MacroOperations)
    include_args: list[str] = field(default_factory=list)
    not_applied: list[str] = field(default_factory=list)
    no_semantic_effect: list[str] = field(default_factory=list)
    char_signedness: CharSignedness | None = None

    def option(self, flag: str, value: str, spelling: str) -> None:
        if flag == DEFINE_FLAG:
            self.macros.define(DEFINE_FLAG + value, spelling)
        elif flag == UNDEFINE_FLAG:
            self.undefine(value, spelling)
        elif flag == INCLUDE_DIR_FLAG:
            self.include_args.append(INCLUDE_DIR_FLAG + value)
        else:
            raise ValueError(f"no CBMC translation for compile option {flag!r}")

    def undefine(self, name: str, spelling: str) -> None:
        if not self.macros.undefine(name.strip()):
            self.not_applied.append(spelling)

    def flag(self, flag: str) -> None:
        joined = _joined_option(flag)
        if flag.startswith(STANDARD_FLAG_PREFIX):
            self.standard = flag.removeprefix(STANDARD_FLAG_PREFIX)
        elif flag == ANSI_FLAG:
            self.standard = ANSI_STANDARD
        elif flag in CHAR_SIGNEDNESS_FLAGS:
            self.char_signedness = CHAR_SIGNEDNESS_FLAGS[flag]
        elif joined is not None:
            self.option(joined, flag[len(joined) :], flag)
        elif has_no_semantic_effect(flag):
            self.no_semantic_effect.append(flag)
        else:
            self.not_applied.append(flag)

    def semantic_args(self, mode: StandardMode | None) -> list[str]:
        args = []
        if mode is not None:
            args.append(mode.flag)
        if self.char_signedness is CharSignedness.UNSIGNED:
            args.append(CBMC_UNSIGNED_CHAR_FLAG)
        return args


def platform_architecture(platform: str | None) -> str | None:
    fields = (platform or "").split()
    return fields[PLATFORM_ARCHITECTURE_FIELD] if len(fields) > PLATFORM_ARCHITECTURE_FIELD else None


def macro_name(define_arg: str) -> str:
    return define_arg.removeprefix(DEFINE_FLAG).split("=", 1)[0].strip()


def _joined_option(flag: str) -> str | None:
    for prefix in (DEFINE_FLAG, UNDEFINE_FLAG, INCLUDE_DIR_FLAG):
        if flag.startswith(prefix) and len(flag) > len(prefix):
            return prefix
    return None


def has_no_semantic_effect(flag: str) -> bool:
    if flag.startswith(TOOL_PASSTHROUGH_PREFIXES):
        return False
    return flag in DIAGNOSTICS_ONLY_FLAGS or flag.startswith((WARNING_FLAG_PREFIX, DEBUG_INFO_FLAG_PREFIX))


def _translate_flag_at(translator: _Translator, flags: list[str], position: int) -> int:
    flag = flags[position]
    value = flags[position + 1] if position + 1 < len(flags) else None
    if value is not None and flag in FORWARDED_FLAGS_TAKING_SEPARATE_VALUE:
        translator.option(flag, value, f"{flag} {value}")
        return FLAG_WITH_SEPARATE_VALUE_WIDTH
    if value is not None and flag in UNAPPLIED_FLAGS_TAKING_SEPARATE_VALUE:
        translator.not_applied.append(f"{flag} {value}")
        return FLAG_WITH_SEPARATE_VALUE_WIDTH
    translator.flag(flag)
    return SINGLE_FLAG_WIDTH


def _translate_flags(translator: _Translator, flags: list[str]) -> None:
    position = 0
    while position < len(flags):
        position += _translate_flag_at(translator, flags, position)


def _spec_define_arg(define: str) -> str:
    return define if define.startswith(DEFINE_FLAG) else DEFINE_FLAG + define


def translate_compile_options(compile_config: CompileConfig, standard: str) -> CompileFlagTranslation:
    translator = _Translator(standard=standard)
    _translate_flags(translator, list(compile_config.flags))
    for define in compile_config.defines:
        translator.macros.define(_spec_define_arg(define), define)
    mode = CBMC_STANDARD_MODES.get(translator.standard)
    if mode is None:
        translator.not_applied.append(STANDARD_FLAG_PREFIX + translator.standard)
    return CompileFlagTranslation(
        macro_args=tuple(arg for _, arg in translator.macros.forwarded),
        include_args=tuple(translator.include_args),
        semantic_args=tuple(translator.semantic_args(mode)),
        dropped_defines=tuple(spelling for _, spelling in translator.macros.dropped),
        not_applied=tuple(translator.not_applied),
        no_semantic_effect=tuple(translator.no_semantic_effect),
        standard=translator.standard,
        standard_mode=mode,
        char_signedness=translator.char_signedness,
    )


def cbmc_preprocessor_macro_args(compile_config: CompileConfig, standard: str) -> list[str]:
    translation = translate_compile_options(compile_config, standard)
    mode = translation.standard_mode.flag if translation.standard_mode else CBMC_DEFAULT_MODE
    unsigned_char = translation.char_signedness is CharSignedness.UNSIGNED
    return [
        *CBMC_PREPROCESSOR_DEFINES,
        *([GCC_UNSIGNED_CHAR_FLAG] if unsigned_char else []),
        STANDARD_FLAG_PREFIX + CBMC_PREPROCESSOR_STANDARDS[mode],
        *translation.macro_args,
    ]
