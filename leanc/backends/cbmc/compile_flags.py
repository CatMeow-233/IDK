from __future__ import annotations

import platform
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from leanc.specs.compile_options import (
    CHAR_SIGNEDNESS_FLAGS,
    SIGNED_CHAR_ARCHITECTURES,
    CharSignedness,
    has_no_semantic_effect,
)
from leanc.specs.model import CompileConfig

DEFINE_FLAG = "-D"
UNDEFINE_FLAG = "-U"
INCLUDE_DIR_FLAG = "-I"
LONG_DEFINE_FLAG = "--define-macro="
LONG_UNDEFINE_FLAG = "--undefine-macro="
LONG_INCLUDE_DIR_FLAG = "--include-directory="
STANDARD_FLAG_PREFIX = "-std="
LONG_STANDARD_FLAG_PREFIX = "--std="
CBMC_UNSIGNED_CHAR_FLAG = "--unsigned-char"
DEFINES_DISABLING_HARNESS_ASSERTIONS = frozenset({"NDEBUG"})

C89_MODE = "--c89"
C99_MODE = "--c99"
C11_MODE = "--c11"
CBMC_DEFAULT_MODE = C11_MODE
CBMC_PREPROCESSOR_STANDARDS: dict[str, str] = {C89_MODE: "gnu89", C99_MODE: "gnu99", C11_MODE: "gnu11"}
CBMC_CPROVER_DEFINE = "-D__CPROVER__"
GCC_UNSIGNED_CHAR_FLAG = "-funsigned-char"
C11_REVISION_NOTE = (
    "CBMC has no C17 mode; C17 only resolves defects of C11, but __STDC_VERSION__ is 201112L instead of 201710L"
)

SIGNED_CHAR_REQUEST = "-fsigned-char"
PLATFORM_ARCHITECTURE_FIELD = 1

_X86_64_DEFINES: tuple[str, ...] = (
    "-D__LP64__",
    "-D__x86_64",
    "-D__x86_64__",
    "-D_LP64",
    "-D__amd64__",
    "-D__amd64",
)
_PPC64LE_DEFINES: tuple[str, ...] = (
    "-D__powerpc",
    "-D__powerpc__",
    "-D__POWERPC__",
    "-D__ppc__",
    "-D__powerpc64",
    "-D__powerpc64__",
    "-D__PPC64__",
    "-D__ppc64__",
    "-D_CALL_ELF=2",
    "-D__LITTLE_ENDIAN__",
)

_SYSTEM_ALIASES: dict[str, str] = {
    "linux": "linux",
    "darwin": "darwin",
    "macos": "darwin",
    "windows": "windows",
    "win32": "windows",
    "cygwin": "windows",
    "msys": "windows",
}
_ARCHITECTURE_ALIASES: dict[str, str] = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "x64": "x86_64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "arm64e": "arm64",
    "i386": "i386",
    "i686": "i386",
    "x86": "i386",
    "riscv64": "riscv64",
    "ppc64le": "ppc64le",
    "powerpc64le": "ppc64le",
    "s390x": "s390x",
    "arm": "arm",
    "armv7l": "arm",
    "armhf": "arm",
    "armel": "arm",
}


@dataclass(frozen=True)
class CbmcHostProfile:
    system: str
    architecture: str
    preprocessor: str
    base_args: tuple[str, ...]
    default_unsigned_char: bool
    arch_defines: tuple[str, ...]


CBMC_HOST_PROFILES: dict[tuple[str, str], CbmcHostProfile] = {
    ("linux", "x86_64"): CbmcHostProfile(
        "linux", "x86_64", "gcc", (CBMC_CPROVER_DEFINE, "-m64"), False, _X86_64_DEFINES
    ),
    ("linux", "arm64"): CbmcHostProfile(
        "linux", "arm64", "gcc", (CBMC_CPROVER_DEFINE,), True, ("-D__aarch64__",)
    ),
    ("linux", "i386"): CbmcHostProfile(
        "linux", "i386", "gcc", (CBMC_CPROVER_DEFINE, "-m32"), False, ("-Di386", "-D__i386", "-D__i386__")
    ),
    ("linux", "riscv64"): CbmcHostProfile(
        "linux", "riscv64", "gcc", (CBMC_CPROVER_DEFINE,), True, ("-D__riscv",)
    ),
    ("linux", "ppc64le"): CbmcHostProfile(
        "linux", "ppc64le", "gcc", (CBMC_CPROVER_DEFINE, "-m64"), True, _PPC64LE_DEFINES
    ),
    ("linux", "s390x"): CbmcHostProfile(
        "linux", "s390x", "gcc", (CBMC_CPROVER_DEFINE, "-m64"), True, ("-D__s390x__",)
    ),
    ("linux", "arm"): CbmcHostProfile(
        "linux", "arm", "gcc", (CBMC_CPROVER_DEFINE,), True, ("-D__arm__",)
    ),
    ("darwin", "arm64"): CbmcHostProfile(
        "darwin", "arm64", "clang", (CBMC_CPROVER_DEFINE,), False, ("-D__aarch64__",)
    ),
    ("darwin", "x86_64"): CbmcHostProfile(
        "darwin", "x86_64", "clang", (CBMC_CPROVER_DEFINE, "-m64"), False, (*_X86_64_DEFINES, "-D__LITTLE_ENDIAN__")
    ),
    ("windows", "x86_64"): CbmcHostProfile(
        "windows", "x86_64", "gcc", (CBMC_CPROVER_DEFINE, "-m64", "-fshort-wchar"), False, ("-D_M_X64", "-D_M_AMD64")
    ),
    ("windows", "arm64"): CbmcHostProfile(
        "windows", "arm64", "gcc", (CBMC_CPROVER_DEFINE, "-fshort-wchar"), True, ("-D_M_ARM64",)
    ),
}


def normalize_host_system(system: str | None = None) -> str:
    raw = (platform.system() if system is None else system).strip().lower()
    if raw.startswith("linux"):
        return "linux"
    return _SYSTEM_ALIASES.get(raw, raw)


def normalize_host_architecture(architecture: str | None = None) -> str:
    raw = (platform.machine() if architecture is None else architecture).strip().lower()
    return _ARCHITECTURE_ALIASES.get(raw, raw)


def cbmc_host_profile(system: str | None = None, architecture: str | None = None) -> CbmcHostProfile | None:
    key = (normalize_host_system(system), normalize_host_architecture(architecture))
    return CBMC_HOST_PROFILES.get(key)


@dataclass(frozen=True)
class StandardMode:
    flag: str
    approximation: str | None = None


CBMC_STANDARD_MODES: dict[str, StandardMode] = {
    **dict.fromkeys(("gnu89", "gnu90"), StandardMode(C89_MODE)),
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

    def define(self, value: str, spelling: str) -> None:
        self.macros.define(DEFINE_FLAG + value, spelling)

    def undefine(self, value: str, spelling: str) -> None:
        if not self.macros.undefine(value.strip()):
            self.not_applied.append(spelling)

    def include_dir(self, value: str, spelling: str) -> None:
        self.include_args.append(INCLUDE_DIR_FLAG + value)

    def select_standard(self, value: str, spelling: str) -> None:
        self.standard = value

    def flag(self, flag: str) -> None:
        joined = _joined_option(flag)
        if joined is not None:
            JOINED_OPTIONS[joined](self, flag[len(joined) :], flag)
        elif flag in CHAR_SIGNEDNESS_FLAGS:
            self.char_signedness = CHAR_SIGNEDNESS_FLAGS[flag]
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


OptionHandler = Callable[[_Translator, str, str], None]
JOINED_OPTIONS: dict[str, OptionHandler] = {
    DEFINE_FLAG: _Translator.define,
    LONG_DEFINE_FLAG: _Translator.define,
    UNDEFINE_FLAG: _Translator.undefine,
    LONG_UNDEFINE_FLAG: _Translator.undefine,
    INCLUDE_DIR_FLAG: _Translator.include_dir,
    LONG_INCLUDE_DIR_FLAG: _Translator.include_dir,
    STANDARD_FLAG_PREFIX: _Translator.select_standard,
    LONG_STANDARD_FLAG_PREFIX: _Translator.select_standard,
}
_MACRO_NAME_END_RE = re.compile(r"[=(]")


def platform_architecture(platform: str | None) -> str | None:
    fields = (platform or "").split()
    return fields[PLATFORM_ARCHITECTURE_FIELD] if len(fields) > PLATFORM_ARCHITECTURE_FIELD else None


def macro_name(define_arg: str) -> str:
    return _MACRO_NAME_END_RE.split(define_arg.removeprefix(DEFINE_FLAG), maxsplit=1)[0].strip()


def _joined_option(flag: str) -> str | None:
    for prefix in JOINED_OPTIONS:
        if flag.startswith(prefix) and len(flag) > len(prefix):
            return prefix
    return None


def _spec_define_arg(define: str) -> str:
    return define if define.startswith(DEFINE_FLAG) else DEFINE_FLAG + define


def translate_compile_options(compile_config: CompileConfig, standard: str) -> CompileFlagTranslation:
    translator = _Translator(standard=standard)
    for flag in compile_config.flags:
        translator.flag(flag)
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


def cbmc_preprocessor_macro_args(
    compile_config: CompileConfig,
    standard: str,
    *,
    system: str | None = None,
    architecture: str | None = None,
) -> list[str]:
    profile = cbmc_host_profile(system, architecture)
    if profile is None:
        host_sys = normalize_host_system(system)
        host_arch = normalize_host_architecture(architecture)
        raise ValueError(f"unsupported CBMC host platform/architecture for macro mirror: {host_sys}/{host_arch}")
    translation = translate_compile_options(compile_config, standard)
    mode = translation.standard_mode.flag if translation.standard_mode else CBMC_DEFAULT_MODE
    unsigned_char = profile.default_unsigned_char or (translation.char_signedness is CharSignedness.UNSIGNED)
    return [
        *profile.base_args,
        *([GCC_UNSIGNED_CHAR_FLAG] if unsigned_char else []),
        STANDARD_FLAG_PREFIX + CBMC_PREPROCESSOR_STANDARDS[mode],
        *translation.macro_args,
        *profile.arch_defines,
    ]
