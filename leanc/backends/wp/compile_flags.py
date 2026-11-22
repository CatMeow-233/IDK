from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from leanc.backends.wp.argv import FRAMA_C_STANDARD_ARG, MACHDEP
from leanc.specs.compile_options import (
    CharSignedness,
    has_no_semantic_effect,
    is_char_signedness_option,
    is_standard_option,
    requested_char_signedness,
    requested_standard,
)

FRAMA_C_STANDARD = FRAMA_C_STANDARD_ARG.removeprefix("-std=")
FRAMA_C_STANDARD_SPELLINGS = frozenset({FRAMA_C_STANDARD, "c1x", "iso9899:2011"})
MACHDEP_CHAR_SIGNEDNESS: dict[str, CharSignedness] = {"x86_64": CharSignedness.SIGNED}
COMPILE_FLAGS_DETAIL = "compile_flags"


class FlagApplication(str, Enum):
    APPLIED = "applied"
    NO_SEMANTIC_EFFECT = "no_semantic_effect"
    NOT_APPLIED = "not_applied"


@dataclass(frozen=True)
class WpCompileFlags:
    machdep: str
    machdep_char_signedness: CharSignedness | None
    applied: tuple[str, ...]
    no_semantic_effect: tuple[str, ...]
    not_applied: tuple[str, ...]

    def details(self) -> dict:
        return {
            "machdep": self.machdep,
            "machdep_char_signedness": self.machdep_char_signedness.value if self.machdep_char_signedness else None,
            FlagApplication.APPLIED.value: list(self.applied),
            FlagApplication.NO_SEMANTIC_EFFECT.value: list(self.no_semantic_effect),
            FlagApplication.NOT_APPLIED.value: list(self.not_applied),
        }


def _char_signedness_matches_machdep(flags: list[str], machdep_signedness: CharSignedness | None) -> bool:
    return machdep_signedness is not None and requested_char_signedness(flags) is machdep_signedness


def _standard_matches_frama_c(flags: list[str]) -> bool:
    return requested_standard(flags) in FRAMA_C_STANDARD_SPELLINGS


def _application(flag: str, *, char_applied: bool, standard_applied: bool) -> FlagApplication:
    if has_no_semantic_effect(flag):
        return FlagApplication.NO_SEMANTIC_EFFECT
    if is_char_signedness_option(flag) and char_applied:
        return FlagApplication.APPLIED
    if is_standard_option(flag) and standard_applied:
        return FlagApplication.APPLIED
    return FlagApplication.NOT_APPLIED


def classify_wp_compile_flags(flags: list[str], machdep: str = MACHDEP) -> WpCompileFlags:
    machdep_signedness = MACHDEP_CHAR_SIGNEDNESS.get(machdep)
    char_applied = _char_signedness_matches_machdep(flags, machdep_signedness)
    standard_applied = _standard_matches_frama_c(flags)
    by_application: dict[FlagApplication, list[str]] = {application: [] for application in FlagApplication}
    for flag in flags:
        by_application[_application(flag, char_applied=char_applied, standard_applied=standard_applied)].append(flag)
    return WpCompileFlags(
        machdep=machdep,
        machdep_char_signedness=machdep_signedness,
        applied=tuple(by_application[FlagApplication.APPLIED]),
        no_semantic_effect=tuple(by_application[FlagApplication.NO_SEMANTIC_EFFECT]),
        not_applied=tuple(by_application[FlagApplication.NOT_APPLIED]),
    )


def unapplied_compile_flags(details: dict) -> list[str]:
    return list((details.get(COMPILE_FLAGS_DETAIL) or {}).get(FlagApplication.NOT_APPLIED.value) or [])
