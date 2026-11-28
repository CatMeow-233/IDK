from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from leanc.backends.clang import define_args, sandbox_visible_path
from leanc.backends.flag_arity import probe_flag_arity
from leanc.backends.object_tools import (
    DEMANGLER_CANDIDATES, NM_CANDIDATES, find_section_tools, first_tool,
    missing_section_tool_message, object_format,
)
from leanc.backends.stack_check import StackCheck
from leanc.backends.verifier_macros import (
    CLANG_DUMP_LABEL, NO_GCC_PREPROCESSED_GATE, collect_divergences, compile_gate_macro_dump,
    gcc_preprocessed_verifiers, needs_frama_c_builtin_macros, plan_verifier_macro_dumps,
)
from leanc.backends.wp.builtin_macros import builtin_macros_path, materialize_builtin_macros
from leanc.foundation.models import (
    Diagnostic, FailureReport, Location, PolicyViolation, StageResult, ToolInvocation,
)
from leanc.foundation.redact import redact, redacted_excerpt
from leanc.foundation.runner import Limits
from leanc.foundation.status import Outcome, Status, outcome_to_status
from leanc.foundation.text import safe_name
from leanc.lang.c_lexer import CSource
from leanc.lang.cpp_lexer import CppSource
from leanc.rules.c_names import ALLOWED_IMPORTS
from leanc.rules.c_rules import (
    RULES, c_header_facts, check_object_symbols, parse_macho_nm, parse_nm,
    parse_section_names, preprocessed_violations, section_name_violations, strip_leading_underscore,
)
from leanc.rules.cpp_rules import (
    CPP_ALLOWED_RUNTIME_IMPORTS, CPP_RULES, CPP_RUNTIME_IMPORT_FAMILIES,
    check_cpp_object_symbols, cpp_preprocessed_violations, cpp_source_violations,
    defines_target, header_facts,
)
from leanc.rules.findings import dedupe_key, unique_id, violation
from leanc.rules.predefined_macros import MacroDefinition, divergent_macro_use_violations, parse_predefined_macros
from leanc.rules.preprocessor import has_linemarker_for
from leanc.rules.source import source_violations
from leanc.rules.symbol_types import STRONG_TEXT_TYPE, UNDEFINED_TYPES, defined_elsewhere, is_defined_global
from leanc.toolchain.context import BackendContext
from leanc.toolchain.cxx import compiler
from leanc.toolchain.tools import find_tool

__all__ = ["STAGE", "TOOL", "run_cpp_integrity", "run_integrity"]

STAGE = "integrity"
TOOL = "leanc-integrity"
TOOL_TIMEOUT_S = 60
MAX_TOOL_OUTPUT_BYTES = 16 * 1024 * 1024
UNRANKED_RULE = 99
STDERR_SEPARATOR = "\n--- stderr ---\n"
NO_LINEMARKER = "the output has no GNU linemarker for the file, so the after-expansion rules would see none of its code"
NULL_SECTION = "NULL"

GCC_CANDIDATES = ("gcc",)
PREDEFINED_MACROS_STEP = "predefined-macros"
C_SYMBOL_EVIDENCE_RULES = frozenset({"SYMBOL_OVERRIDE", "DANGEROUS_IMPORT"})
CPP_SYMBOL_EVIDENCE_RULES = frozenset({"SYMBOL_OVERRIDE", "DANGEROUS_IMPORT", "EXCEPTIONS", "CPP_RUNTIME", "TERMINATION"})


def _find(names: tuple[str, ...]) -> str | None:
    return first_tool(names, find_tool)


def _object_path(object_root: Path, rel: str) -> Path:
    return object_root / (rel[:-2] + ".o" if rel.endswith(".c") else rel + ".o")


@dataclass
class _CheckedFile:
    source: CSource
    entry: dict
    symbols: list | None = None


class _IntegrityGate(ABC):
    rules: ClassVar[dict[str, str]]
    compiler_name: ClassVar[str]
    stack_violation_reason: ClassVar[str]
    symbol_evidence_rules: ClassVar[frozenset[str]]
    code_under_test: ClassVar[str]
    passed_prefix: ClassVar[str]

    def __init__(self, ctx: BackendContext, started: float, compiler_path: str | None) -> None:
        self.ctx = ctx
        self.spec = ctx.spec
        self.started = started
        self.compiler_path = compiler_path
        self.snapshot = Path(os.path.realpath(ctx.snapshot_dir))
        self.object_root = ctx.build_dir / "obj"
        self.raw_root = ctx.raw_dir / STAGE
        self.raw_root.mkdir(parents=True, exist_ok=True)
        self.nm = _find(NM_CANDIDATES)
        self.section_tools = find_section_tools(find_tool)
        self.readelf = self.section_tools.readelf
        self.limits = Limits(timeout_s=ctx.timeout(TOOL_TIMEOUT_S), max_output_bytes=MAX_TOOL_OUTPUT_BYTES)
        self.details: dict[str, Any] = {}
        self.invocations: list[ToolInvocation] = []
        self.problems: list[str] = []
        self.timeouts: list[str] = []
        self.raw_indices: dict[str, int] = {}
        self.violations: list[PolicyViolation] = []
        self.raw_refs: dict[tuple[str, str], str] = {}
        self.headers: dict[str, str] = {}
        self.checked: dict[str, _CheckedFile] = {}

    @abstractmethod
    def lex(self, text: str) -> CSource: ...

    @abstractmethod
    def source_violations(self, rel: str, text: str) -> list[PolicyViolation]: ...

    @abstractmethod
    def preprocessed_violations(self, rel: str, preprocessed: str) -> list[PolicyViolation]: ...

    @abstractmethod
    def collect_symbols(self, rel: str, real_object: str, checked: _CheckedFile) -> None: ...

    @abstractmethod
    def defined_names(self, symbols: list) -> set[str]: ...

    @abstractmethod
    def symbol_violations(self, rel: str, checked: _CheckedFile, elsewhere: set[str]) -> list[PolicyViolation]: ...

    @abstractmethod
    def object_defines_target(self, symbols: list) -> bool: ...

    @abstractmethod
    def missing_target_message(self) -> str: ...

    @abstractmethod
    def symbol_evidence(self, file: str) -> str | None: ...

    def run(self) -> StageResult:
        self.load_headers()
        self.check_flag_arity()
        for rel in self.spec.sources.editable:
            self.check_file(rel)
        self.check_symbols_across_files()
        self.check_predefined_macro_divergence()
        return self.report()

    def load_headers(self) -> None:
        for header in self.spec.sources.headers:
            path = self.snapshot / header
            if path.is_file():
                self.headers[header] = path.read_text(encoding="utf-8", errors="replace")
            else:
                self.problems.append(f"protected header {header} is missing from the snapshot")

    def run_tool(self, tool: str, argv: list[str], rel: str, what: str, stdin: bytes | None = None) -> str | None:
        proc = self.ctx.run(argv, limits=self.limits, cwd=self.snapshot, stdin_data=stdin)
        self.invocations.append(proc.to_invocation(tool, self.ctx.run_dir, self.ctx.log_name(STAGE, f"{what}-{rel}")))
        raw = self.raw_root / f"{what}-{self.raw_indices.setdefault(rel, len(self.raw_indices)):02d}-{safe_name(rel)}.txt"
        raw.write_text(redact(proc.stdout + (STDERR_SEPARATOR + proc.stderr if proc.stderr else "")), encoding="utf-8")
        self.raw_refs[(rel, what)] = self.ctx.relrun(raw)
        if proc.timed_out:
            self.timeouts.append(f"{what} of {rel} timed out")
            return None
        if not proc.ok or proc.output_truncated:
            reason = proc.error or ("output truncated" if proc.output_truncated else f"exit {proc.exit_code}")
            self.problems.append(f"{what} of {rel} failed ({reason}): {redacted_excerpt(proc.stderr)}")
            return None
        return proc.stdout

    def check_flag_arity(self) -> None:
        if not self.compiler_path:
            return
        probed = probe_flag_arity(self.ctx, self.compiler_path, self.compiler_name, self.limits, STAGE)
        self.details["flag_arity"] = probed.details(self.compiler_path)
        self.problems += probed.problems
        self.timeouts += probed.timeouts
        self.invocations += probed.invocations

    def check_file(self, rel: str) -> None:
        entry: dict[str, Any] = {"source": rel, "checks": [], "argv": {}}
        self.details["files"].append(entry)
        path = self.snapshot / rel
        if not path.is_file():
            self.problems.append(f"editable source {rel} is missing from the snapshot")
            return
        text = path.read_text(encoding="utf-8", errors="replace")
        checked = _CheckedFile(source=self.lex(text), entry=entry)
        self.checked[rel] = checked
        self.violations += self.source_violations(rel, text)
        entry["checks"].append("source")
        if self.compiler_path:
            self.check_preprocessed(rel, entry)
            self.check_unbounded_stack_allocations(rel, entry)
        else:
            self.problems.append(f"{self.compiler_name} not found: cannot check the preprocessed source")
        object_file = _object_path(self.object_root, rel)
        if not object_file.is_file():
            self.problems.append(f"object file for {rel} not found ({object_file}); the compile gate must run first")
            return
        real_object = os.path.realpath(object_file)
        self.collect_symbols(rel, real_object, checked)
        self.check_sections(rel, real_object, checked)

    def check_preprocessed(self, rel: str, entry: dict) -> None:
        argv = [
            self.compiler_path,
            f"-std={self.spec.language.standard}",
            *self.spec.compile.flags,
            *define_args(self.spec.compile.defines),
            *self.ctx.include_args(),
            "-E",
            rel,
        ]
        entry["argv"]["preprocess"] = argv
        preprocessed = self.run_tool(self.compiler_name, argv, rel, "preprocess")
        if preprocessed is None:
            return
        if not has_linemarker_for(rel, preprocessed):
            self.problems.append(f"preprocess of {rel}: {NO_LINEMARKER}")
            return
        self.violations += self.preprocessed_violations(rel, preprocessed)
        entry["checks"].append("preprocessed")

    def check_unbounded_stack_allocations(self, rel: str, entry: dict) -> None:
        check = StackCheck(
            self.ctx, self.compiler_path, self.compiler_name, self.limits, STAGE, self.stack_violation_reason
        )
        outcome, checked = check.run(rel)
        entry["argv"].update(outcome.argv)
        self.violations += outcome.violations
        self.problems += outcome.problems
        self.timeouts += outcome.timeouts
        self.invocations += outcome.invocations
        if checked:
            entry["checks"].append("stack")

    def check_predefined_macro_divergence(self) -> None:
        if not gcc_preprocessed_verifiers(self.spec):
            self.details["predefined_macros"] = {"checked": False, "reason": NO_GCC_PREPROCESSED_GATE}
            return
        gcc = _find(GCC_CANDIDATES)
        frama_c_macros = sandbox_visible_path(builtin_macros_path(self.ctx.build_dir))
        plan = plan_verifier_macro_dumps(self.spec, frama_c_macros, gcc=gcc, clang=self.compiler_path)
        if plan.problems:
            self.problems.extend(plan.problems)
            return
        if not self.compiler_path or not self.frama_c_builtin_macros_ready():
            return
        clang_argv = compile_gate_macro_dump(self.compiler_path, self.spec, self.ctx.include_args())
        self.details["predefined_macros"] = {"checked": False, "argv": {CLANG_DUMP_LABEL: clang_argv}}
        self.details["predefined_macros"]["argv"].update({dump.label: dump.argv for dump in plan.dumps})
        clang_macros = self.predefined_macros(self.compiler_name, CLANG_DUMP_LABEL, clang_argv)
        divergent = None if clang_macros is None else collect_divergences(clang_macros, plan.dumps, self.predefined_macros)
        if divergent is None:
            return
        self.details["predefined_macros"].update({"checked": True, "divergent": len(divergent)})
        for rel, checked in self.checked.items():
            self.violations += divergent_macro_use_violations(rel, checked.source, divergent)

    def frama_c_builtin_macros_ready(self) -> bool:
        if not needs_frama_c_builtin_macros(self.spec):
            return True
        built = materialize_builtin_macros(self.ctx, self.limits, STAGE)
        self.invocations += built.invocations
        self.problems += built.problems
        self.timeouts += built.timeouts
        return built.available

    def predefined_macros(self, tool: str, label: str, argv: list[str]) -> dict[str, MacroDefinition] | None:
        dump = self.run_tool(tool, argv, label, PREDEFINED_MACROS_STEP)
        if dump is None:
            return None
        parsed = parse_predefined_macros(dump)
        if parsed.unparsed or not parsed.definitions:
            self.problems.append(f"{tool} printed an unexpected predefined-macro list: {parsed.unparsed[:1]!r}")
            return None
        return parsed.definitions

    def list_object_symbols(self, rel: str, real_object: str, checked: _CheckedFile) -> tuple[list[tuple[str, str]] | None, bool]:
        fmt = object_format(real_object)
        if fmt == "bitcode":
            self.problems.append(f"object file for {rel} is LLVM bitcode, not a machine-code object file")
            return None, False
        is_macho = fmt == "macho"
        argv = [self.nm, "-m" if is_macho else "-P", real_object]
        checked.entry["argv"]["nm"] = argv
        listing = self.run_tool("nm", argv, rel, "nm")
        if listing is None:
            return None, is_macho
        symbols = strip_leading_underscore(parse_macho_nm(listing)) if is_macho else parse_nm(listing)
        return symbols, is_macho

    def check_sections(self, rel: str, real_object: str, checked: _CheckedFile) -> None:
        fmt = object_format(real_object)
        if fmt == "bitcode":
            self.problems.append(f"sections of {rel} could not be checked: object file is LLVM bitcode")
            return
        tool = self.section_tools.select(fmt)
        if tool is None:
            self.problems.append(missing_section_tool_message(fmt))
            return
        argv = tool.argv(real_object)
        checked.entry["argv"][tool.name] = argv
        sections = self.run_tool(tool.name, argv, rel, "sections")
        if sections is not None:
            names = parse_section_names(sections, tool.fmt)
            checked.entry["sections"] = [name for name in names if name != NULL_SECTION]
            self.violations += section_name_violations(rel, names, checked.source)
            checked.entry["checks"].append("sections")

    def check_symbols_across_files(self) -> None:
        defined_per_file = {rel: self.defined_names(file.symbols or []) for rel, file in self.checked.items()}
        for rel, file in self.checked.items():
            if file.symbols is not None:
                self.violations += self.symbol_violations(rel, file, defined_elsewhere(defined_per_file, rel))
        every_file_has_symbols = all(file.symbols is not None for file in self.checked.values())
        if self.checked and every_file_has_symbols and not any(
            self.object_defines_target(file.symbols) for file in self.checked.values()
        ):
            self.violations.append(violation("MISSING_TARGET", self.missing_target_message(), None, None))

    def ranked_violations(self) -> list[PolicyViolation]:
        rank = {rule: position for position, rule in enumerate(self.rules)}
        unique: dict[tuple, PolicyViolation] = {}
        for found in self.violations:
            unique.setdefault(dedupe_key(found), found)
        return sorted(
            unique.values(),
            key=lambda found: (found.file or "", found.line or 0, rank.get(found.rule, UNRANKED_RULE), found.message),
        )

    def failure_report(self, found: PolicyViolation, location: Location | None, taken_ids: set[str]) -> FailureReport:
        evidence = self.symbol_evidence(found.file or "") if found.rule in self.symbol_evidence_rules else None
        return FailureReport(
            id=unique_id(f"{STAGE}:{found.rule}:{found.file or '-'}:{found.line or 0}", taken_ids),
            kind="policy",
            tool=TOOL,
            property_id=found.rule,
            description=found.message,
            location=location,
            explanation=self.rules.get(found.rule, ""),
            raw_ref=evidence,
        )

    def report(self) -> StageResult:
        ranked = self.ranked_violations()
        self.details["violations"] = len(ranked)
        self.details["problems"] = self.problems + self.timeouts
        diagnostics: list[Diagnostic] = []
        failures: list[FailureReport] = []
        taken_ids: set[str] = set()
        for found in ranked:
            location = Location(file=found.file, line=found.line) if found.file else None
            diagnostics.append(
                Diagnostic(tool=TOOL, rule_id=found.rule, severity="error", message=found.message, location=location)
            )
            failures.append(self.failure_report(found, location, taken_ids))

        def result(outcome: Outcome, summary: str, status: Status | None) -> StageResult:
            return StageResult(
                stage=STAGE,
                outcome=outcome,
                status=status,
                summary=summary,
                diagnostics=diagnostics,
                failures=failures,
                invocations=self.invocations,
                details=self.details,
                duration_s=round(time.monotonic() - self.started, 3),
            )

        if ranked:
            return result(Outcome.FAIL, self.violation_summary(ranked), Status.STATIC_ANALYSIS_WARNING)
        if self.timeouts:
            return result(Outcome.TIMEOUT, "; ".join(self.timeouts), outcome_to_status(STAGE, Outcome.TIMEOUT))
        if self.problems:
            return result(
                Outcome.ERROR,
                "source-integrity checks could not run: " + "; ".join(self.problems),
                outcome_to_status(STAGE, Outcome.ERROR),
            )
        checked = ", ".join(f"{file['source']} ({'/'.join(file['checks'])})" for file in self.details["files"])
        return result(Outcome.PASS, f"{self.passed_prefix}: {checked}", None)

    def violation_summary(self, ranked: list[PolicyViolation]) -> str:
        rules = ", ".join(dict.fromkeys(found.rule for found in ranked))
        unfinished = self.problems + self.timeouts
        incomplete = f"; some checks could not run: {'; '.join(unfinished)}" if unfinished else ""
        return (
            f"{len(ranked)} source-integrity violation(s) ({rules}): {self.code_under_test} tampers with or diverges "
            f"between the verification tools{incomplete}"
        )


class _CIntegrityGate(_IntegrityGate):
    rules = RULES
    compiler_name = "clang"
    stack_violation_reason = "CBMC cannot model stack exhaustion"
    symbol_evidence_rules = C_SYMBOL_EVIDENCE_RULES
    code_under_test = "the code under test"
    passed_prefix = "source integrity OK"

    def __init__(self, ctx: BackendContext, started: float) -> None:
        super().__init__(ctx, started, ctx.tools.clang)
        self.details.update(
            {
                "tools": {"clang": ctx.tools.clang, "nm": self.nm, "readelf": self.readelf},
                "rules": sorted(RULES),
                "allowed_imports": sorted(ALLOWED_IMPORTS),
                "files": [],
                "violations": 0,
            }
        )
        self.header_identifiers: set[str] = set()

    def load_headers(self) -> None:
        super().load_headers()
        self.header_identifiers = c_header_facts(self.headers).idents

    def lex(self, text: str) -> CSource:
        return CSource(text)

    def source_violations(self, rel: str, text: str) -> list[PolicyViolation]:
        return source_violations(self.spec, rel, text, headers=self.headers)

    def preprocessed_violations(self, rel: str, preprocessed: str) -> list[PolicyViolation]:
        return preprocessed_violations(rel, preprocessed)

    def collect_symbols(self, rel: str, real_object: str, checked: _CheckedFile) -> None:
        if not self.nm:
            self.problems.append("nm / llvm-nm not found: cannot check object symbols")
            return
        symbols, _ = self.list_object_symbols(rel, real_object, checked)
        if symbols is None:
            return
        checked.symbols = symbols
        checked.entry["defined"] = sorted(name for name, kind in symbols if is_defined_global(kind))
        checked.entry["undefined"] = sorted(name for name, kind in symbols if kind in UNDEFINED_TYPES)
        checked.entry["checks"].append("symbols")

    def defined_names(self, symbols: list) -> set[str]:
        return {name for name, kind in symbols if is_defined_global(kind)}

    def symbol_violations(self, rel: str, checked: _CheckedFile, elsewhere: set[str]) -> list[PolicyViolation]:
        return check_object_symbols(
            self.spec,
            rel,
            checked.symbols,
            header_idents=self.header_identifiers,
            defined_elsewhere=elsewhere,
            src=checked.source,
        )

    def object_defines_target(self, symbols: list) -> bool:
        return (self.spec.target.function, STRONG_TEXT_TYPE) in symbols

    def missing_target_message(self) -> str:
        return f"no editable object defines the target function '{self.spec.target.function}' (as a global function)"

    def symbol_evidence(self, file: str) -> str | None:
        return self.raw_refs.get((file, "nm"))


class _CppIntegrityGate(_IntegrityGate):
    rules = CPP_RULES
    compiler_name = "clang++"
    stack_violation_reason = "a stack allocation of input-dependent size"
    symbol_evidence_rules = CPP_SYMBOL_EVIDENCE_RULES
    code_under_test = "the C++ code under test"
    passed_prefix = "source integrity OK (C++)"

    def __init__(self, ctx: BackendContext, started: float, compiler_path: str | None) -> None:
        super().__init__(ctx, started, compiler_path)
        self.demangler = _find(DEMANGLER_CANDIDATES)
        self.details.update(
            {
                "language": "c++",
                "tools": {"clang++": compiler_path, "nm": self.nm, "readelf": self.readelf, "demangler": self.demangler},
                "rules": sorted(CPP_RULES),
                "allowed_imports": sorted(ALLOWED_IMPORTS),
                "allowed_cpp_runtime_imports": sorted(CPP_ALLOWED_RUNTIME_IMPORTS) + list(CPP_RUNTIME_IMPORT_FAMILIES),
                "files": [],
                "violations": 0,
            }
        )
        self.facts = None

    def load_headers(self) -> None:
        super().load_headers()
        self.facts = header_facts(self.headers)

    def lex(self, text: str) -> CSource:
        return CppSource(text)

    def source_violations(self, rel: str, text: str) -> list[PolicyViolation]:
        return cpp_source_violations(self.spec, rel, text, headers=self.headers)

    def preprocessed_violations(self, rel: str, preprocessed: str) -> list[PolicyViolation]:
        return cpp_preprocessed_violations(rel, preprocessed, self.facts)

    def demangled_symbols(
        self, rel: str, symbols: list[tuple[str, str]], *, no_strip_underscore: bool = False
    ) -> list[tuple[str, str, str]] | None:
        names = [name for name, _ in symbols]
        if not names:
            return []
        stdin = ("\n".join(names) + "\n").encode()
        argv = [self.demangler, "-n"] if no_strip_underscore else [self.demangler]
        listing = self.run_tool("c++filt", argv, rel, "demangle", stdin)
        if listing is None:
            return None
        demangled = listing.split("\n")[: len(names)]
        if len(demangled) != len(names) or any(not line.strip() for line in demangled):
            self.problems.append(f"demangling the symbols of {rel} produced unexpected output")
            return None
        return [(name, kind, line.strip()) for (name, kind), line in zip(symbols, demangled)]

    def collect_symbols(self, rel: str, real_object: str, checked: _CheckedFile) -> None:
        if not (self.nm and self.demangler):
            self.problems.append(
                "nm / llvm-nm or a C++ demangler (llvm-cxxfilt / c++filt) not found: cannot check object symbols"
            )
            return
        raw_symbols, is_macho = self.list_object_symbols(rel, real_object, checked)
        if raw_symbols is None:
            return
        triples = self.demangled_symbols(rel, raw_symbols, no_strip_underscore=is_macho)
        if triples is None:
            return
        checked.symbols = triples
        checked.entry["defined"] = sorted(shown for _, kind, shown in triples if is_defined_global(kind))
        checked.entry["undefined"] = sorted(shown for _, kind, shown in triples if kind in UNDEFINED_TYPES)
        checked.entry["checks"].append("symbols")

    def defined_names(self, symbols: list) -> set[str]:
        return {name for name, kind, _ in symbols if kind not in UNDEFINED_TYPES}

    def symbol_violations(self, rel: str, checked: _CheckedFile, elsewhere: set[str]) -> list[PolicyViolation]:
        return check_cpp_object_symbols(
            self.spec, rel, checked.symbols, facts=self.facts, defined_elsewhere=elsewhere, src=checked.source
        )

    def object_defines_target(self, symbols: list) -> bool:
        return defines_target(symbols, self.spec.target.function)

    def missing_target_message(self) -> str:
        return (
            f"no editable object defines the target function '{self.spec.target.function}' "
            "(as an external function or member function)"
        )

    def symbol_evidence(self, file: str) -> str | None:
        return self.raw_refs.get((file, "demangle")) or self.raw_refs.get((file, "nm"))


def run_integrity(ctx: BackendContext) -> StageResult:
    if ctx.spec.language.is_cpp:
        return run_cpp_integrity(ctx)
    return _CIntegrityGate(ctx, time.monotonic()).run()


def run_cpp_integrity(ctx: BackendContext) -> StageResult:
    started = time.monotonic()
    return _CppIntegrityGate(ctx, started, compiler(ctx)).run()
