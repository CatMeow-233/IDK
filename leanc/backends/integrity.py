from __future__ import annotations

import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from leanc.foundation.models import Diagnostic, FailureReport, Location, PolicyViolation, StageResult, ToolInvocation
from leanc.foundation.redact import redact
from leanc.foundation.runner import Limits
from leanc.foundation.status import Outcome, Status, outcome_to_status
from leanc.lang.c_lexer import CSource
from leanc.lang.cpp_lexer import CppSource
from leanc.rules.c_names import ALLOWED_IMPORTS
from leanc.rules.c_rules import (
    RULES,
    SECTION_RE,
    c_header_facts,
    check_object_symbols,
    parse_nm,
    preprocessed_violations,
    section_violations,
)
from leanc.rules.cpp_rules import (
    CPP_ALLOWED_RUNTIME_IMPORTS,
    CPP_RULES,
    _defines_target,
    check_cpp_object_symbols,
    cpp_preprocessed_violations,
    cpp_source_violations,
    header_facts,
)
from leanc.rules.findings import violation
from leanc.rules.source import source_violations
from leanc.rules.symbol_types import STRONG_TEXT_TYPE, UNDEFINED_TYPES
from leanc.toolchain.context import BackendContext
from leanc.toolchain.cxx import compiler
from leanc.toolchain.tools import find_tool

__all__ = ["STAGE", "TOOL", "run_cpp_integrity", "run_integrity"]

STAGE = "integrity"
TOOL = "leanc-integrity"
TOOL_TIMEOUT_S = 60
MAX_TOOL_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_STDERR_EXCERPT_CHARS = 300
UNRANKED_RULE = 99
STDERR_SEPARATOR = "\n--- stderr ---\n"
UNDEFINED_SYMBOL = "U"
NULL_SECTION = "NULL"

NM_CANDIDATES = ("llvm-nm-18", "llvm-nm", "nm")
READELF_CANDIDATES = ("llvm-readelf-18", "llvm-readelf", "readelf")
DEMANGLER_CANDIDATES = ("llvm-cxxfilt-18", "llvm-cxxfilt", "c++filt")
UNBOUNDED_STACK_CHECK_FLAGS = (
    "-fsyntax-only",
    "-fno-color-diagnostics",
    "-fno-caret-diagnostics",
    "-Wno-everything",
    "-Wvla",
    "-Walloca",
    "-Werror=vla",
    "-Werror=alloca",
)
C_SYMBOL_EVIDENCE_RULES = frozenset({"SYMBOL_OVERRIDE", "DANGEROUS_IMPORT"})
CPP_SYMBOL_EVIDENCE_RULES = frozenset({"SYMBOL_OVERRIDE", "DANGEROUS_IMPORT", "EXCEPTIONS", "CPP_RUNTIME", "TERMINATION"})
CPP_RUNTIME_IMPORT_FAMILIES = (
    "global operator new/delete",
    "std::nothrow",
    "std::__throw_*",
    "std::__glibcxx_assert_fail",
    "vtable for __cxxabiv1::__*class_type_info",
    "std::terminate/__cxa_begin_catch (from __clang_call_terminate)",
)

_STACK_DIAG_RE = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):\d+: (?:error|warning): (?P<msg>.*?)\[-W(?:error,)?-?W?(?:vla|alloca)[^\]]*\]",
    re.M,
)


def _find(names: tuple[str, ...]) -> str | None:
    for name in names:
        path = find_tool(name)
        if path:
            return path
    return None


def _is_defined_global(symbol_type: str) -> bool:
    return symbol_type.isupper() and symbol_type != UNDEFINED_SYMBOL


def _object_path(object_root: Path, rel: str) -> Path:
    return object_root / (rel[:-2] + ".o" if rel.endswith(".c") else rel + ".o")


def _define_args(defines: list[str]) -> list[str]:
    return [define if define.startswith("-D") else f"-D{define}" for define in defines]


def _defined_elsewhere(defined_per_file: dict[str, set[str]], rel: str) -> set[str]:
    if len(defined_per_file) <= 1:
        return set()
    return set().union(*(names for other, names in defined_per_file.items() if other != rel))


def _dedupe_key(found: PolicyViolation) -> tuple:
    return (found.rule, found.file, found.line) if found.line else (found.rule, found.file, found.message)


def _unique_id(base: str, taken: set[str]) -> str:
    candidate, suffix = base, 2
    while candidate in taken:
        candidate, suffix = f"{base}#{suffix}", suffix + 1
    taken.add(candidate)
    return candidate


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
        self.readelf = _find(READELF_CANDIDATES)
        self.limits = Limits(timeout_s=ctx.timeout(TOOL_TIMEOUT_S), max_output_bytes=MAX_TOOL_OUTPUT_BYTES)
        self.details: dict[str, Any] = {}
        self.invocations: list[ToolInvocation] = []
        self.problems: list[str] = []
        self.timeouts: list[str] = []
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
    def defines_target(self, symbols: list) -> bool: ...

    @abstractmethod
    def missing_target_message(self) -> str: ...

    @abstractmethod
    def symbol_evidence(self, file: str) -> str | None: ...

    def run(self) -> StageResult:
        self.load_headers()
        for rel in self.spec.sources.editable:
            self.check_file(rel)
        self.check_symbols_across_files()
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
        raw = self.raw_root / f"{what}-{rel.replace('/', '_')}.txt"
        raw.write_text(redact(proc.stdout + (STDERR_SEPARATOR + proc.stderr if proc.stderr else "")), encoding="utf-8")
        self.raw_refs[(rel, what)] = self.ctx.relrun(raw)
        if proc.timed_out:
            self.timeouts.append(f"{what} of {rel} timed out")
            return None
        if not proc.ok or proc.output_truncated:
            reason = proc.error or ("output truncated" if proc.output_truncated else f"exit {proc.exit_code}")
            self.problems.append(f"{what} of {rel} failed ({reason}): {self._stderr_excerpt(proc.stderr)}")
            return None
        return proc.stdout

    @staticmethod
    def _stderr_excerpt(stderr: str) -> str:
        return redact(stderr.strip())[:MAX_STDERR_EXCERPT_CHARS]

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
            *_define_args(self.spec.compile.defines),
            *self.ctx.include_args(),
            "-E",
            rel,
        ]
        entry["argv"]["preprocess"] = argv
        preprocessed = self.run_tool(self.compiler_name, argv, rel, "preprocess")
        if preprocessed is not None:
            self.violations += self.preprocessed_violations(rel, preprocessed)
            entry["checks"].append("preprocessed")

    def check_unbounded_stack_allocations(self, rel: str, entry: dict) -> None:
        argv = [
            self.compiler_path,
            f"-std={self.spec.language.standard}",
            *_define_args(self.spec.compile.defines),
            *self.ctx.include_args(),
            *UNBOUNDED_STACK_CHECK_FLAGS,
            rel,
        ]
        entry["argv"]["stack"] = argv
        proc = self.ctx.run(argv, limits=self.limits, cwd=self.snapshot)
        log_name = self.ctx.log_name(STAGE, f"stack-{rel}")
        self.invocations.append(proc.to_invocation(self.compiler_name, self.ctx.run_dir, log_name))
        if proc.timed_out:
            self.timeouts.append(f"stack check of {rel} timed out")
            return
        if proc.error:
            self.problems.append(f"stack check of {rel} failed: {proc.error}")
            return
        hits = 0
        for diagnostic in _STACK_DIAG_RE.finditer(proc.stderr):
            hits += 1
            message = f"{diagnostic.group('msg').strip()} ({self.stack_violation_reason})"
            self.violations.append(violation("UNBOUNDED_STACK", message, rel, int(diagnostic.group("line"))))
        if proc.exit_code not in (0, None) and not hits:
            self.problems.append(
                f"stack check of {rel} failed (exit {proc.exit_code}): {self._stderr_excerpt(proc.stderr)}"
            )
        entry["checks"].append("stack")

    def check_sections(self, rel: str, real_object: str, checked: _CheckedFile) -> None:
        if not self.readelf:
            self.problems.append("readelf / llvm-readelf not found: cannot check object sections")
            return
        argv = [self.readelf, "-S", "-W", real_object]
        checked.entry["argv"]["readelf"] = argv
        sections = self.run_tool("readelf", argv, rel, "sections")
        if sections is not None:
            names = dict.fromkeys(SECTION_RE.findall(sections))
            checked.entry["sections"] = [name for name in names if name != NULL_SECTION]
            self.violations += section_violations(rel, sections, checked.source)
            checked.entry["checks"].append("sections")

    def check_symbols_across_files(self) -> None:
        defined_per_file = {rel: self.defined_names(file.symbols or []) for rel, file in self.checked.items()}
        for rel, file in self.checked.items():
            if file.symbols is not None:
                self.violations += self.symbol_violations(rel, file, _defined_elsewhere(defined_per_file, rel))
        every_file_has_symbols = all(file.symbols is not None for file in self.checked.values())
        if self.checked and every_file_has_symbols and not any(
            self.defines_target(file.symbols) for file in self.checked.values()
        ):
            self.violations.append(violation("MISSING_TARGET", self.missing_target_message(), None, None))

    def ranked_violations(self) -> list[PolicyViolation]:
        rank = {rule: position for position, rule in enumerate(self.rules)}
        unique: dict[tuple, PolicyViolation] = {}
        for found in self.violations:
            unique.setdefault(_dedupe_key(found), found)
        return sorted(
            unique.values(),
            key=lambda found: (found.file or "", found.line or 0, rank.get(found.rule, UNRANKED_RULE), found.message),
        )

    def failure_report(self, found: PolicyViolation, location: Location | None, taken_ids: set[str]) -> FailureReport:
        evidence = self.symbol_evidence(found.file or "") if found.rule in self.symbol_evidence_rules else None
        return FailureReport(
            id=_unique_id(f"{STAGE}:{found.rule}:{found.file or '-'}:{found.line or 0}", taken_ids),
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
        argv = [self.nm, "-P", real_object]
        checked.entry["argv"]["nm"] = argv
        listing = self.run_tool("nm", argv, rel, "nm")
        if listing is None:
            return
        symbols = parse_nm(listing)
        checked.symbols = symbols
        checked.entry["defined"] = sorted(name for name, kind in symbols if _is_defined_global(kind))
        checked.entry["undefined"] = sorted(name for name, kind in symbols if kind in UNDEFINED_TYPES)
        checked.entry["checks"].append("symbols")

    def defined_names(self, symbols: list) -> set[str]:
        return {name for name, kind in symbols if _is_defined_global(kind)}

    def symbol_violations(self, rel: str, checked: _CheckedFile, elsewhere: set[str]) -> list[PolicyViolation]:
        return check_object_symbols(
            self.spec,
            rel,
            checked.symbols,
            header_idents=self.header_identifiers,
            defined_elsewhere=elsewhere,
            src=checked.source,
        )

    def defines_target(self, symbols: list) -> bool:
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
        self.facts = header_facts({})

    def load_headers(self) -> None:
        super().load_headers()
        self.facts = header_facts(self.headers)

    def lex(self, text: str) -> CSource:
        return CppSource(text)

    def source_violations(self, rel: str, text: str) -> list[PolicyViolation]:
        return cpp_source_violations(self.spec, rel, text, headers=self.headers)

    def preprocessed_violations(self, rel: str, preprocessed: str) -> list[PolicyViolation]:
        return cpp_preprocessed_violations(rel, preprocessed, self.facts)

    def demangled_symbols(self, rel: str, symbols: list[tuple[str, str]]) -> list[tuple[str, str, str]] | None:
        names = [name for name, _ in symbols]
        if not names:
            return []
        stdin = ("\n".join(names) + "\n").encode()
        listing = self.run_tool("c++filt", [self.demangler], rel, "demangle", stdin)
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
        argv = [self.nm, "-P", real_object]
        checked.entry["argv"]["nm"] = argv
        listing = self.run_tool("nm", argv, rel, "nm")
        if listing is None:
            return
        triples = self.demangled_symbols(rel, parse_nm(listing))
        if triples is None:
            return
        checked.symbols = triples
        checked.entry["defined"] = sorted(shown for _, kind, shown in triples if _is_defined_global(kind))
        checked.entry["undefined"] = sorted(shown for _, kind, shown in triples if kind in UNDEFINED_TYPES)
        checked.entry["checks"].append("symbols")

    def defined_names(self, symbols: list) -> set[str]:
        return {name for name, kind, _ in symbols if kind not in UNDEFINED_TYPES}

    def symbol_violations(self, rel: str, checked: _CheckedFile, elsewhere: set[str]) -> list[PolicyViolation]:
        return check_cpp_object_symbols(
            self.spec, rel, checked.symbols, facts=self.facts, defined_elsewhere=elsewhere, src=checked.source
        )

    def defines_target(self, symbols: list) -> bool:
        return _defines_target(symbols, self.spec.target.function)

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
