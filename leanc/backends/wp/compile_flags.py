from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from leanc.backends.wp.argv import FRAMA_C_STANDARD_ARG, MACHDEP
from leanc.backends.wp.build_target import BuildTarget
from leanc.specs.compile_options import (
    CharSignedness,
    default_char_signedness,
    has_no_semantic_effect,
    is_char_signedness_option,
    is_standard_option,
    requested_char_signedness,
    requested_standard,
)

FRAMA_C_STANDARD = FRAMA_C_STANDARD_ARG.removeprefix("-std=")
C11_SPELLINGS = frozenset({FRAMA_C_STANDARD, "c1x", "iso9899:2011"})
C17_SPELLINGS = frozenset({"c17", "c18", "iso9899:2017", "iso9899:2018"})
FRAMA_C_PARSED_STANDARDS = C11_SPELLINGS | C17_SPELLINGS
MACHDEP_CHAR_SIGNEDNESS: dict[str, CharSignedness] = {"x86_64": CharSignedness.SIGNED}
COMPILE_FLAGS_DETAIL = "compile_flags"
NOT_KNOWN_SIGNEDNESS = "not known to be signed"
UNKNOWN_SIGNEDNESS = "unknown"
UNKNOWN_BUILD_TARGET_ENTRY = "(build target unknown: {problem})"
TARGET_DEFAULT_CHAR_ENTRY = "(default char of build target {triple}: {target}; Frama-C machdep {machdep}: {machdep_char})"


class FlagApplication(str, Enum):
    APPLIED = "applied"
    NO_SEMANTIC_EFFECT = "no_semantic_effect"
    NOT_APPLIED = "not_applied"


def _signedness_text(signedness: CharSignedness | None, unknown: str) -> str:
    return signedness.value if signedness else unknown


@dataclass(frozen=True)
class WpCompileFlags:
    machdep: str
    machdep_char_signedness: CharSignedness | None
    build_target: BuildTarget
    build_char_signedness: CharSignedness | None
    applied: tuple[str, ...]
    no_semantic_effect: tuple[str, ...]
    not_applied: tuple[str, ...]

    def details(self) -> dict:
        return {
            "machdep": self.machdep,
            "machdep_char_signedness": _signedness_text(self.machdep_char_signedness, UNKNOWN_SIGNEDNESS),
            "build_target": self.build_target.details(),
            "build_char_signedness": _signedness_text(self.build_char_signedness, NOT_KNOWN_SIGNEDNESS),
            FlagApplication.APPLIED.value: list(self.applied),
            FlagApplication.NO_SEMANTIC_EFFECT.value: list(self.no_semantic_effect),
            FlagApplication.NOT_APPLIED.value: list(self.not_applied),
        }


@dataclass(frozen=True)
class _CharSemantics:
    build: CharSignedness | None
    machdep: CharSignedness | None
    explicit: bool

    @property
    def applied(self) -> bool:
        return self.machdep is not None and self.build is self.machdep


def _char_semantics(flags: list[str], target: BuildTarget, machdep: str) -> _CharSemantics:
    explicit = requested_char_signedness(flags)
    build = explicit if explicit is not None else default_char_signedness(target.architecture)
    return _CharSemantics(build=build, machdep=MACHDEP_CHAR_SIGNEDNESS.get(machdep), explicit=explicit is not None)


def _standard_parsed_by_frama_c(flags: list[str]) -> bool:
    return requested_standard(flags) in FRAMA_C_PARSED_STANDARDS


def _application(flag: str, *, char_applied: bool, standard_applied: bool) -> FlagApplication:
    if has_no_semantic_effect(flag):
        return FlagApplication.NO_SEMANTIC_EFFECT
    if is_char_signedness_option(flag) and char_applied:
        return FlagApplication.APPLIED
    if is_standard_option(flag) and standard_applied:
        return FlagApplication.APPLIED
    return FlagApplication.NOT_APPLIED


def _build_target_entries(target: BuildTarget, char: _CharSemantics, machdep: str) -> list[str]:
    if target.triple is None:
        return [UNKNOWN_BUILD_TARGET_ENTRY.format(problem=target.problem)]
    if char.explicit or char.applied:
        return []
    return [
        TARGET_DEFAULT_CHAR_ENTRY.format(
            triple=target.triple,
            target=_signedness_text(char.build, NOT_KNOWN_SIGNEDNESS),
            machdep=machdep,
            machdep_char=_signedness_text(char.machdep, UNKNOWN_SIGNEDNESS),
        )
    ]


def classify_wp_compile_flags(flags: list[str], target: BuildTarget, machdep: str = MACHDEP) -> WpCompileFlags:
    char = _char_semantics(flags, target, machdep)
    standard_applied = _standard_parsed_by_frama_c(flags)
    by_application: dict[FlagApplication, list[str]] = {application: [] for application in FlagApplication}
    for flag in flags:
        by_application[_application(flag, char_applied=char.applied, standard_applied=standard_applied)].append(flag)
    return WpCompileFlags(
        machdep=machdep,
        machdep_char_signedness=char.machdep,
        build_target=target,
        build_char_signedness=char.build,
        applied=tuple(by_application[FlagApplication.APPLIED]),
        no_semantic_effect=tuple(by_application[FlagApplication.NO_SEMANTIC_EFFECT]),
        not_applied=(*by_application[FlagApplication.NOT_APPLIED], *_build_target_entries(target, char, machdep)),
    )


def unapplied_compile_flags(details: dict) -> list[str]:
    return list((details.get(COMPILE_FLAGS_DETAIL) or {}).get(FlagApplication.NOT_APPLIED.value) or [])
