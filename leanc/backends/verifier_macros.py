from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from leanc.backends.cbmc.compile_flags import (
    cbmc_host_profile,
    cbmc_preprocessor_macro_args,
    normalize_host_architecture,
    normalize_host_system,
)
from leanc.backends.clang import define_args
from leanc.backends.wp.argv import frama_c_preprocessor_macro_args
from leanc.rules.predefined_macros import (
    GCC_MISSING,
    PREDEFINED_MACRO_DUMP_ARGS,
    MacroDefinition,
    MacroDivergence,
    divergent_macros,
)
from leanc.specs.model import Spec

NO_GCC_PREPROCESSED_GATE = "no gate preprocesses the code with gcc (cbmc, wp)"
CLANG_DUMP_LABEL = "clang"
CLANG_TOOL = "clang"
GCC_TOOL = "gcc"
CLANG_MISSING = (
    "clang not found: cannot compare the macros clang predefines for the compile gate with those "
    "clang predefines when preprocessing for CBMC"
)

MacroReader = Callable[[str, str, list[str]], "dict[str, MacroDefinition] | None"]


@dataclass(frozen=True)
class VerifierPreprocessor:
    gate: str
    verifier: str
    macro_args: Callable[[Spec, str], list[str]]


def _cbmc_macro_args(spec: Spec, _frama_c_builtin_macros: str) -> list[str]:
    return cbmc_preprocessor_macro_args(spec.compile, spec.language.standard)


CBMC_GATE = "cbmc"
FRAMA_C_GATE = "wp"
VERIFIER_PREPROCESSORS: tuple[VerifierPreprocessor, ...] = (
    VerifierPreprocessor(CBMC_GATE, "CBMC", _cbmc_macro_args),
    VerifierPreprocessor(FRAMA_C_GATE, "Frama-C", frama_c_preprocessor_macro_args),
)


@dataclass(frozen=True)
class MacroDump:
    label: str
    verifier: str
    argv: list[str]
    tool: str = GCC_TOOL


@dataclass(frozen=True)
class VerifierMacroPlan:
    dumps: list[MacroDump]
    problems: list[str]


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


def _cbmc_macro_dump(
    spec: Spec,
    *,
    gcc: str | None,
    clang: str | None,
    system: str | None,
    architecture: str | None,
) -> tuple[MacroDump | None, str | None]:
    profile = cbmc_host_profile(system, architecture)
    if profile is None:
        host_sys = normalize_host_system(system)
        host_arch = normalize_host_architecture(architecture)
        return None, f"unsupported CBMC host platform/architecture for macro mirror: {host_sys}/{host_arch}"
    tool_name = profile.preprocessor
    executable = clang if tool_name == CLANG_TOOL else gcc
    if executable is None:
        return None, CLANG_MISSING if tool_name == CLANG_TOOL else GCC_MISSING
    args = cbmc_preprocessor_macro_args(
        spec.compile, spec.language.standard, system=system, architecture=architecture
    )
    return MacroDump(CBMC_GATE, "CBMC", [executable, *args, *PREDEFINED_MACRO_DUMP_ARGS], tool=tool_name), None


def plan_verifier_macro_dumps(
    spec: Spec,
    frama_c_builtin_macros: str,
    *,
    gcc: str | None,
    clang: str | None,
    system: str | None = None,
    architecture: str | None = None,
) -> VerifierMacroPlan:
    dumps: list[MacroDump] = []
    problems: list[str] = []
    for preprocessor in gcc_preprocessed_verifiers(spec):
        if preprocessor.gate == CBMC_GATE:
            dump, problem = _cbmc_macro_dump(
                spec, gcc=gcc, clang=clang, system=system, architecture=architecture
            )
            if problem is not None:
                problems.append(problem)
            elif dump is not None:
                dumps.append(dump)
        elif gcc is None:
            problems.append(GCC_MISSING)
        else:
            args = preprocessor.macro_args(spec, frama_c_builtin_macros)
            dumps.append(MacroDump(preprocessor.gate, preprocessor.verifier, [gcc, *args, *PREDEFINED_MACRO_DUMP_ARGS]))
    return VerifierMacroPlan(dumps=dumps, problems=list(dict.fromkeys(problems)))


def collect_divergences(
    clang: dict[str, MacroDefinition], dumps: list[MacroDump], read: MacroReader
) -> dict[str, MacroDivergence] | None:
    divergent: dict[str, MacroDivergence] = {}
    for dump in dumps:
        verifier_macros = read(dump.tool, dump.label, dump.argv)
        if verifier_macros is None:
            return None
        for name, found in divergent_macros(clang, verifier_macros, dump.verifier).items():
            divergent.setdefault(name, found)
    return divergent
