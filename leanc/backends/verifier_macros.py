from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from leanc.backends.cbmc.compile_flags import cbmc_preprocessor_macro_args
from leanc.backends.clang import define_args
from leanc.backends.wp.argv import frama_c_preprocessor_macro_args
from leanc.rules.predefined_macros import (
    PREDEFINED_MACRO_DUMP_ARGS,
    MacroDefinition,
    MacroDivergence,
    divergent_macros,
)
from leanc.specs.model import Spec

NO_GCC_PREPROCESSED_GATE = "no gate preprocesses the code with gcc (cbmc, wp)"
CLANG_DUMP_LABEL = "clang"
GCC_TOOL = "gcc"

MacroReader = Callable[[str, str, list[str]], "dict[str, MacroDefinition] | None"]


@dataclass(frozen=True)
class VerifierPreprocessor:
    gate: str
    verifier: str
    macro_args: Callable[[Spec, str], list[str]]


def _cbmc_macro_args(spec: Spec, _frama_c_builtin_macros: str) -> list[str]:
    return cbmc_preprocessor_macro_args(spec.compile, spec.language.standard)


FRAMA_C_GATE = "wp"
VERIFIER_PREPROCESSORS: tuple[VerifierPreprocessor, ...] = (
    VerifierPreprocessor("cbmc", "CBMC", _cbmc_macro_args),
    VerifierPreprocessor(FRAMA_C_GATE, "Frama-C", frama_c_preprocessor_macro_args),
)


@dataclass(frozen=True)
class MacroDump:
    label: str
    verifier: str
    argv: list[str]


def gcc_preprocessed_verifiers(spec: Spec) -> list[VerifierPreprocessor]:
    return [preprocessor for preprocessor in VERIFIER_PREPROCESSORS if preprocessor.gate in spec.gates]


def compile_gate_macro_dump(compiler: str, spec: Spec, include_args: list[str]) -> list[str]:
    return [
        compiler,
        f"-std={spec.language.standard}",
        *spec.compile.flags,
        *define_args(spec.compile.defines),
        *include_args,
        *PREDEFINED_MACRO_DUMP_ARGS,
    ]


def needs_frama_c_builtin_macros(spec: Spec) -> bool:
    return FRAMA_C_GATE in spec.gates


def verifier_macro_dumps(gcc: str, spec: Spec, frama_c_builtin_macros: str) -> list[MacroDump]:
    return [
        MacroDump(p.gate, p.verifier, [gcc, *p.macro_args(spec, frama_c_builtin_macros), *PREDEFINED_MACRO_DUMP_ARGS])
        for p in gcc_preprocessed_verifiers(spec)
    ]


def collect_divergences(
    clang: dict[str, MacroDefinition], dumps: list[MacroDump], read: MacroReader
) -> dict[str, MacroDivergence] | None:
    divergent: dict[str, MacroDivergence] = {}
    for dump in dumps:
        gcc = read(GCC_TOOL, dump.label, dump.argv)
        if gcc is None:
            return None
        for name, found in divergent_macros(clang, gcc, dump.verifier).items():
            divergent.setdefault(name, found)
    return divergent
