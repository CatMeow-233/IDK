from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from leanc.backends.cbmc.harness import find_probes
from leanc.backends.inductive.annotation import (
    EditableUnit,
    claimed_entries,
    function_probe_labels,
    loop_probe_labels,
    match_contracts,
    vacuity_text,
    verify_text,
)
from leanc.backends.inductive.harness_calls import (
    ENTRY_POINT,
    CallSite,
    HarnessLayout,
    ReturnKind,
    harness_layout,
    instrumented_harness,
    return_probe_label,
    body_references,
)
from leanc.backends.inductive.loops import GotoLoop, instrument, listed_loops, residual_loops
from leanc.backends.inductive.preprocess import (
    PreprocessedUnit,
    UnitRole,
    inductive_models_path,
    preprocess_unit,
)
from leanc.backends.inductive.problems import BuildProblem, contract_rejected, inconclusive, tool_error
from leanc.backends.inductive.program import (
    TRUSTED_HARNESS_STEM,
    TRUSTED_MODELS_STEM,
    BuildMode,
    LinkedProgram,
    TrustedUnit,
    compile_editable,
    compile_trusted,
    editable_stem,
    link_program,
    undeclared_problem,
    write_unit,
)
from leanc.backends.inductive.steps import StepRun, ToolSteps
from leanc.backends.inductive.symbols import claimed_problems, return_kinds
from leanc.foundation.models import ToolInvocation
from leanc.foundation.reasons import Reason
from leanc.lang.c_lexer import CSource
from leanc.rules.loop_contracts import LoopContracts, parse_loop_contracts
from leanc.rules.loop_sites import FunctionEntry, MatchedLoop
from leanc.rules.preprocessor import defined_macro_names
from leanc.specs.model import InductiveConfig
from leanc.toolchain.context import BackendContext
from leanc.toolchain.cprover import cprover_pin

BUILD_DIR_NAME = "inductive"
SHARED_UNITS_DIR = "units"
INSTRUMENTED_PROGRAM = "dfcc.goto"


@dataclass(frozen=True)
class BuiltProgram:
    mode: BuildMode
    linked: Path
    instrumented: Path


@dataclass
class InductiveBuild:
    problem: BuildProblem | None = None
    invocations: list[ToolInvocation] = field(default_factory=list)
    argv: dict[str, list[str]] = field(default_factory=dict)
    contracts: tuple[MatchedLoop, ...] = ()
    claimed_calls: tuple[CallSite, ...] = ()
    returns: Mapping[str, ReturnKind] = field(default_factory=dict)
    function_probes: tuple[str, ...] = ()
    loop_probes: tuple[str, ...] = ()
    return_probes: tuple[str, ...] = ()
    verify: BuiltProgram | None = None
    vacuity: BuiltProgram | None = None
    bodiless: tuple[str, ...] = ()
    cprover: str = ""

    @property
    def built(self) -> bool:
        return self.problem is None and self.verify is not None and self.vacuity is not None

    def details(self) -> dict[str, Any]:
        return {
            "cprover": self.cprover,
            "argv": dict(self.argv),
            "contracts": [item.contract.location for item in self.contracts],
            "claimed_calls": [{"function": call.function, "location": call.where} for call in self.claimed_calls],
            "probes": {
                "function": list(self.function_probes),
                "loop": list(self.loop_probes),
                "return": list(self.return_probes),
            },
            "unmodelled_bodiless_after_dfcc": list(self.bodiless),
        }


def _missing_inputs(ctx: BackendContext, inductive: InductiveConfig) -> list[str]:
    inputs = [inductive.harness, *ctx.spec.sources.editable]
    return [path for path in inputs if not (ctx.snapshot_dir / path).is_file()]


def _snapshot_text(ctx: BackendContext, rel: str) -> str:
    return (ctx.snapshot_dir / rel).read_text(encoding="utf-8", errors="replace")


def _spec_problem(ctx: BackendContext) -> BuildProblem | None:
    spec = ctx.spec
    if spec.inductive is None:
        return tool_error("the spec has no inductive section")
    if spec.verification is None:
        return tool_error("the inductive gate needs the verification section (timeout_s, memory_mb, unwind)")
    if spec.verification.malloc_may_fail:
        return tool_error("verification.malloc_may_fail is not supported by the inductive gate")
    return None


@dataclass
class _Builder:
    ctx: BackendContext
    inductive: InductiveConfig
    steps: ToolSteps
    result: InductiveBuild
    root: Path
    contracts: LoopContracts = field(default_factory=LoopContracts)
    editable: list[EditableUnit] = field(default_factory=list)
    harness: dict[BuildMode, PreprocessedUnit] = field(default_factory=dict)
    layouts: dict[BuildMode, HarnessLayout] = field(default_factory=dict)
    models_text: str = ""
    models_unit: TrustedUnit | None = None
    entries: tuple[FunctionEntry, ...] = ()

    @property
    def claimed(self) -> list[str]:
        return list(self.inductive.functions)

    def mode_dir(self, mode: BuildMode) -> Path:
        return self.root / mode.value

    def load_contracts(self) -> BuildProblem | None:
        path = self.ctx.snapshot_dir / self.inductive.contracts
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
        macro_names: set[str] = set()
        for rel in self.ctx.spec.sources.editable:
            macro_names |= defined_macro_names(CSource(_snapshot_text(self.ctx, rel)).code)
        self.contracts = parse_loop_contracts(self.ctx.spec, text, macro_names=frozenset(macro_names))
        if self.contracts.accepted:
            return None
        return contract_rejected(
            f"the loop contracts in {self.inductive.contracts} are rejected",
            [problem.describe() for problem in self.contracts.problems],
        )

    def preprocess(self) -> BuildProblem | None:
        for index, rel in enumerate(self.ctx.spec.sources.editable):
            unit = preprocess_unit(self.steps, UnitRole.EDITABLE, rel)
            if isinstance(unit, BuildProblem):
                return unit
            self.editable.append(EditableUnit(index, rel, unit.text, _snapshot_text(self.ctx, rel)))
        for mode in BuildMode:
            unit = preprocess_unit(self.steps, UnitRole.HARNESS, self.inductive.harness, vacuity=mode is BuildMode.VACUITY)
            if isinstance(unit, BuildProblem):
                return unit
            self.harness[mode] = unit
        models = preprocess_unit(self.steps, UnitRole.MODELS, inductive_models_path())
        if isinstance(models, BuildProblem):
            return models
        self.models_text = models.text
        return None

    def check_editable_entry_point(self) -> BuildProblem | None:
        found = [where for unit in self.editable for where in body_references(unit.pp, ENTRY_POINT)]
        if found:
            return tool_error(f"editable code references {ENTRY_POINT}", found)
        return None

    def check_harness(self) -> BuildProblem | None:
        harness = self.inductive.harness
        has_probes, _ = find_probes(_snapshot_text(self.ctx, harness))
        if not has_probes:
            return tool_error(f"the inductive harness {harness} has no LEANC_REACH probe")
        for mode, unit in self.harness.items():
            layout = harness_layout(unit.text, self.claimed, harness)
            if layout.problems:
                return tool_error(f"the inductive harness {harness} ({mode.value} build) breaks the harness rules", layout.problems)
            self.layouts[mode] = layout
        if self.layouts[BuildMode.VERIFY].call_sequence() != self.layouts[BuildMode.VACUITY].call_sequence():
            return tool_error(f"the inductive harness {harness} makes different claimed calls in its vacuity build")
        self.result.claimed_calls = self.layouts[BuildMode.VERIFY].calls
        return None

    def match_sites(self) -> BuildProblem | None:
        match = match_contracts(self.editable, self.contracts.loops)
        if match.problems:
            return contract_rejected("a loop contract does not match exactly one loop", match.problems)
        entries = claimed_entries(self.editable, self.claimed)
        if entries.problems:
            return tool_error("the claimed functions cannot be located in the editable sources", entries.problems)
        self.result.contracts = match.matched
        self.entries = entries.entries
        self.result.function_probes = tuple(function_probe_labels(self.claimed))
        self.result.loop_probes = tuple(loop_probe_labels(match.matched))
        return None

    def models(self, compiles: list[StepRun]) -> TrustedUnit | BuildProblem:
        if self.models_unit is None:
            path = write_unit(self.root / SHARED_UNITS_DIR, TRUSTED_MODELS_STEM, self.models_text)
            unit = compile_trusted(self.steps, BuildMode.VERIFY, path, compiles)
            if isinstance(unit, BuildProblem):
                return unit
            self.models_unit = unit
        return self.models_unit

    def trusted(self, mode: BuildMode, harness_text: str, compiles: list[StepRun]) -> list[TrustedUnit] | BuildProblem:
        models = self.models(compiles)
        if isinstance(models, BuildProblem):
            return models
        path = write_unit(self.mode_dir(mode), TRUSTED_HARNESS_STEM, harness_text)
        harness = compile_trusted(self.steps, mode, path, compiles)
        if isinstance(harness, BuildProblem):
            return harness
        return [models, harness]

    def link(self, mode: BuildMode, harness_text: str, editable_texts: list[str]) -> LinkedProgram | BuildProblem:
        compiles: list[StepRun] = []
        trusted = self.trusted(mode, harness_text, compiles)
        if isinstance(trusted, BuildProblem):
            return trusted
        directory = self.mode_dir(mode)
        editable = [write_unit(directory, editable_stem(unit.index), text) for unit, text in zip(self.editable, editable_texts)]
        problem = compile_editable(self.steps, mode, editable, bool(self.result.contracts), compiles)
        if problem is not None:
            if mode is BuildMode.VACUITY and problem.reason is Reason.CONTRACT_REJECTED:
                return tool_error(f"the vacuity build failed after the verify build succeeded: {problem.summary}")
            return problem
        return link_program(self.steps, mode, trusted, editable, self.ctx.spec.sources.editable, compiles)

    def checked_link(self, mode: BuildMode, harness_text: str, editable_texts: list[str]) -> LinkedProgram | BuildProblem:
        program = self.link(mode, harness_text, editable_texts)
        if isinstance(program, BuildProblem):
            return program
        claimed = claimed_problems(program.table, self.claimed, self.ctx.spec.sources.editable, self.inductive.harness)
        if claimed:
            return tool_error("the linked program does not bind the claimed functions and main() as required", claimed)
        undeclared = undeclared_problem(program)
        return undeclared if undeclared is not None else program

    def build_verify(self) -> LinkedProgram | BuildProblem:
        harness_text = instrumented_harness(
            self.harness[BuildMode.VERIFY].text, self.layouts[BuildMode.VERIFY], self.inductive.harness
        )
        texts = [verify_text(unit, self.result.contracts) for unit in self.editable]
        return self.checked_link(BuildMode.VERIFY, harness_text, texts)

    def build_vacuity(self, verify: LinkedProgram) -> LinkedProgram | BuildProblem:
        returns = return_kinds(verify.table, self.claimed)
        if isinstance(returns, str):
            return tool_error(returns)
        self.result.returns = returns
        self.result.return_probes = tuple(return_probe_label(name) for name in self.claimed)
        harness_text = instrumented_harness(
            self.harness[BuildMode.VACUITY].text, self.layouts[BuildMode.VACUITY], self.inductive.harness, returns
        )
        texts = [vacuity_text(unit, self.result.contracts, self.entries) for unit in self.editable]
        return self.checked_link(BuildMode.VACUITY, harness_text, texts)

    def instrumented(self, program: LinkedProgram) -> BuiltProgram | BuildProblem:
        output = program.path.parent / INSTRUMENTED_PROGRAM
        bodiless = instrument(self.steps, f"{program.mode.value} dfcc", program.path, output)
        if isinstance(bodiless, BuildProblem):
            return bodiless
        self.result.bodiless = tuple(dict.fromkeys([*self.result.bodiless, *bodiless]))
        loops = listed_loops(self.steps, f"{program.mode.value} loops", output)
        if isinstance(loops, BuildProblem):
            return loops
        residual = residual_loops(loops, self.inductive.harness)
        if residual:
            return _residual_problem(residual)
        return BuiltProgram(program.mode, program.path, output)

    def build(self) -> BuildProblem | None:
        for step in (self.load_contracts, self.preprocess, self.check_editable_entry_point, self.check_harness, self.match_sites):
            problem = step()
            if problem is not None:
                return problem
        verify = self.build_verify()
        if isinstance(verify, BuildProblem):
            return verify
        vacuity = self.build_vacuity(verify)
        if isinstance(vacuity, BuildProblem):
            return vacuity
        built: dict[BuildMode, BuiltProgram] = {}
        for program in (verify, vacuity):
            outcome = self.instrumented(program)
            if isinstance(outcome, BuildProblem):
                return outcome
            built[program.mode] = outcome
        self.result.verify, self.result.vacuity = built[BuildMode.VERIFY], built[BuildMode.VACUITY]
        return None


def _residual_problem(residual: list[GotoLoop]) -> BuildProblem:
    return inconclusive(
        Reason.LOOP_WITHOUT_CONTRACT,
        "after loop-contract instrumentation these loops still have no contract, so CBMC is not run",
        [loop.describe() for loop in residual],
    )


def build_inductive(ctx: BackendContext) -> InductiveBuild:
    result = InductiveBuild()
    problem = _spec_problem(ctx)
    if problem is not None:
        result.problem = problem
        return result
    inductive = ctx.spec.inductive
    pin = cprover_pin(ctx.tools)
    result.cprover = pin.describe()
    if not pin.tested:
        result.problem = tool_error("the inductive gate needs cbmc, goto-cc and goto-instrument of a tested release", pin.problems())
        return result
    missing = _missing_inputs(ctx, inductive)
    if missing:
        result.problem = tool_error("missing input file(s) in the snapshot", missing)
        return result
    steps = ToolSteps.start(ctx)
    builder = _Builder(ctx, inductive, steps, result, Path(os.path.abspath(ctx.build_dir)) / BUILD_DIR_NAME)
    result.problem = builder.build()
    result.invocations = steps.invocations
    result.argv = steps.argv
    return result
