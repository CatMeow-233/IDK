"""Mandatory source-integrity gate (runs at *verify* time, after a passing compile).

Why a separate gate?  The repair policy (:mod:`leanc.repair.policy`) only sees
proposals inside the repair loop; ``leanc verify`` on hand-written or
generated code never ran it, and even inside the loop a count-based lexical
check cannot see what the *tools* see.  Every verifier in the pipeline must
judge the same program, and that program must not be able to tamper with the
verification itself.  Reviewers demonstrated false ``VERIFIED_BOUNDED``
results through exactly those gaps:

* overriding verification hooks: a body for ``__assert_fail`` (every harness
  ``assert()`` becomes a no-op call in CBMC), or for ``nondet_size_t`` (the
  "symbolic" inputs get narrowed);
* tool divergence: ``#ifdef __clang__`` / ``__OPTIMIZE__`` / ``__CPROVER__``
  / ``LEANC_VACUITY_CHECK`` let clang, the sanitizer build and CBMC compile
  *different* code;
* pragmas assembled by macros (``_Pragma(#x)``) that switch CBMC checks off;
* process termination (``exit``/``abort``/``atexit``, an
  ``__attribute__((constructor))`` calling ``_exit(0)``): CBMC prunes the
  paths, the native tests report success, LeakSanitizer never runs;
* weak references (``__VERIFIER_assume`` resolves only under the verifier),
  GNU linemarkers that mark the file as a system header (analyzer findings
  vanish), ...

The gate therefore checks every editable file with **absolute** rules (not
"new relative to the old version") at three levels:

A. the source text (:func:`source_violations`, also used by the repair and
   generation policy): directives, includes, ``_Pragma``, forbidden
   identifiers, attributes and definitions;
B. the preprocessed text (``clang -E`` with the spec's flags), restricted to
   lines that belong to the editable file itself, so macro tricks are seen
   after expansion;
C. the compiled object (``nm``/``readelf``): which symbols it defines, which
   it imports (an **allowlist** of library functions CBMC models faithfully,
   because CBMC treats body-less functions as returning arbitrary values),
   weak symbols and constructor sections.

A violation is a definite, blocking finding about the code under test:
``Outcome.FAIL`` with ``Status.STATIC_ANALYSIS_WARNING``.  A tool problem
(missing ``nm``, unparsable output, ...) is ``Outcome.ERROR`` - never a pass.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from leanc.foundation.models import Diagnostic, FailureReport, Location, PolicyViolation, StageResult
from leanc.foundation.redact import redact
from leanc.foundation.runner import Limits
from leanc.foundation.status import Outcome, Status, outcome_to_status
from leanc.lang.c_lexer import CSource
from leanc.lang.cpp_lexer import CppSource
from leanc.rules.c_rules import (
    _SECTION_RE,
    ALLOWED_IMPORTS,
    RULES,
    _header_facts,
    _v,
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
from leanc.rules.source import source_violations
from leanc.toolchain.context import BackendContext
from leanc.toolchain.cxx import compiler
from leanc.toolchain.tools import find_tool

__all__ = ["STAGE", "TOOL", "run_cpp_integrity", "run_integrity"]

STAGE = "integrity"
TOOL = "leanc-integrity"


def _find(names: tuple[str, ...]) -> str | None:
    for n in names:
        p = find_tool(n)
        if p:
            return p
    return None


_STACK_DIAG_RE = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):\d+: (?:error|warning): (?P<msg>.*?)\[-W(?:error,)?-?W?(?:vla|alloca)[^\]]*\]",
    re.M,
)


def run_integrity(ctx: BackendContext) -> StageResult:
    """Mandatory source-integrity gate; see the module docstring."""
    if ctx.spec.language.is_cpp:  # mangled symbols, C++ runtime, C++ source rules
        return run_cpp_integrity(ctx)
    t0 = time.monotonic()
    spec = ctx.spec
    snap = Path(os.path.realpath(ctx.snapshot_dir))
    obj_root = ctx.build_dir / "obj"
    raw_root = ctx.raw_dir / STAGE
    raw_root.mkdir(parents=True, exist_ok=True)
    nm = _find(("llvm-nm-18", "llvm-nm", "nm"))
    readelf = _find(("llvm-readelf-18", "llvm-readelf", "readelf"))
    details: dict = {
        "tools": {"clang": ctx.tools.clang, "nm": nm, "readelf": readelf},
        "rules": sorted(RULES),
        "allowed_imports": sorted(ALLOWED_IMPORTS),
        "files": [],
        "violations": 0,
    }
    invocations = []
    problems: list[str] = []
    timeouts: list[str] = []
    limits = Limits(timeout_s=ctx.timeout(60), max_output_bytes=16 * 1024 * 1024)
    violations: list[PolicyViolation] = []
    raw_refs: dict[tuple[str, str], str] = {}

    headers: dict[str, str] = {}
    for h in spec.sources.headers:
        p = snap / h
        if p.is_file():
            headers[h] = p.read_text(encoding="utf-8", errors="replace")
        else:
            problems.append(f"protected header {h} is missing from the snapshot")
    header_idents, _ = _header_facts(headers)

    def run(tool: str, argv: list[str], rel: str, what: str) -> str | None:
        proc = ctx.run(argv, limits=limits, cwd=snap)
        invocations.append(proc.to_invocation(tool, ctx.run_dir, ctx.log_name(STAGE, f"{what}-{rel}")))
        raw = raw_root / f"{what}-{rel.replace('/', '_')}.txt"
        raw.write_text(redact(proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else "")),
                       encoding="utf-8")
        raw_refs[(rel, what)] = ctx.relrun(raw)
        if proc.timed_out:
            timeouts.append(f"{what} of {rel} timed out")
            return None
        if not proc.ok or proc.output_truncated:
            reason = proc.error or ("output truncated" if proc.output_truncated else f"exit {proc.exit_code}")
            problems.append(f"{what} of {rel} failed ({reason}): {redact(proc.stderr.strip())[:300]}")
            return None
        return proc.stdout

    per_file: dict[str, dict] = {}
    for rel in spec.sources.editable:
        entry: dict = {"source": rel, "checks": [], "argv": {}}
        details["files"].append(entry)
        path = snap / rel
        if not path.is_file():
            problems.append(f"editable source {rel} is missing from the snapshot")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        src = CSource(text)
        per_file[rel] = {"src": src, "entry": entry}
        violations += source_violations(spec, rel, text, headers=headers)
        entry["checks"].append("source")

        if ctx.tools.clang:
            argv = [
                ctx.tools.clang,
                f"-std={spec.language.standard}",
                *spec.compile.flags,
                *[d if d.startswith("-D") else f"-D{d}" for d in spec.compile.defines],
                *ctx.include_args(),
                "-E",
                rel,
            ]
            entry["argv"]["preprocess"] = argv
            pp = run("clang", argv, rel, "preprocess")
            if pp is not None:
                violations += preprocessed_violations(rel, pp)
                entry["checks"].append("preprocessed")
            # Semantic check the preprocessor cannot do: VLAs (also those hidden
            # behind typedefs or macros) and alloca.  CBMC treats any stack
            # allocation as valid, so they must never reach a verdict.
            sargv = [
                ctx.tools.clang,
                f"-std={spec.language.standard}",
                *[d if d.startswith("-D") else f"-D{d}" for d in spec.compile.defines],
                *ctx.include_args(),
                "-fsyntax-only",
                "-fno-color-diagnostics",
                "-fno-caret-diagnostics",
                "-Wno-everything",
                "-Wvla",
                "-Walloca",
                "-Werror=vla",
                "-Werror=alloca",
                rel,
            ]
            entry["argv"]["stack"] = sargv
            proc = ctx.run(sargv, limits=limits, cwd=snap)
            invocations.append(proc.to_invocation("clang", ctx.run_dir, ctx.log_name(STAGE, f"stack-{rel}")))
            if proc.timed_out:
                timeouts.append(f"stack check of {rel} timed out")
            elif proc.error:
                problems.append(f"stack check of {rel} failed: {proc.error}")
            else:
                hits = 0
                for m in _STACK_DIAG_RE.finditer(proc.stderr):
                    hits += 1
                    line = int(m.group("line"))
                    violations.append(
                        _v("UNBOUNDED_STACK", f"{m.group('msg').strip()} (CBMC cannot model stack exhaustion)",
                           m.group("file") if m.group("file") == rel else rel, line)
                    )
                if proc.exit_code not in (0, None) and not hits:
                    problems.append(f"stack check of {rel} failed (exit {proc.exit_code}): "
                                    f"{redact(proc.stderr.strip())[:300]}")
                entry["checks"].append("stack")
        else:
            problems.append("clang not found: cannot check the preprocessed source")

        obj = obj_root / (rel[:-2] + ".o" if rel.endswith(".c") else rel + ".o")
        if not obj.is_file():
            problems.append(f"object file for {rel} not found ({obj}); the compile gate must run first")
            continue
        real_obj = os.path.realpath(obj)
        if nm:
            argv = [nm, "-P", real_obj]
            entry["argv"]["nm"] = argv
            out = run("nm", argv, rel, "nm")
            if out is not None:
                syms = parse_nm(out)
                per_file[rel]["symbols"] = syms
                entry["defined"] = sorted(n for n, t in syms if t.isupper() and t != "U")
                entry["undefined"] = sorted(n for n, t in syms if t in ("U", "w", "v"))
                entry["checks"].append("symbols")
        else:
            problems.append("nm / llvm-nm not found: cannot check object symbols")
        if readelf:
            argv = [readelf, "-S", "-W", real_obj]
            entry["argv"]["readelf"] = argv
            out = run("readelf", argv, rel, "sections")
            if out is not None:
                entry["sections"] = [s for s in dict.fromkeys(_SECTION_RE.findall(out)) if s != "NULL"]
                violations += section_violations(rel, out, src)
                entry["checks"].append("sections")
        else:
            problems.append("readelf / llvm-readelf not found: cannot check object sections")

    # symbol rules need every object's definitions (calls between editable files are fine)
    all_defined: dict[str, set[str]] = {
        rel: {n for n, t in info.get("symbols", []) if t.isupper() and t not in ("U",)} for rel, info in per_file.items()
    }
    target_defined = any(
        (spec.target.function, "T") in info.get("symbols", []) for info in per_file.values()
    )
    for rel, info in per_file.items():
        if "symbols" not in info:
            continue
        elsewhere = set().union(*(d for r, d in all_defined.items() if r != rel)) if len(all_defined) > 1 else set()
        violations += check_object_symbols(
            spec, rel, info["symbols"], header_idents=header_idents, defined_elsewhere=elsewhere, src=info["src"]
        )
    if per_file and all("symbols" in i for i in per_file.values()) and not target_defined:
        violations.append(_v("MISSING_TARGET", f"no editable object defines the target function "
                             f"'{spec.target.function}' (as a global function)", None, None))

    # report
    order = {r: k for k, r in enumerate(RULES)}
    uniq: dict[tuple, PolicyViolation] = {}
    for v in violations:
        uniq.setdefault((v.rule, v.file, v.line) if v.line else (v.rule, v.file, v.message), v)
    vs = sorted(uniq.values(), key=lambda v: (v.file or "", v.line or 0, order.get(v.rule, 99), v.message))
    details["violations"] = len(vs)
    details["problems"] = problems + timeouts
    diagnostics: list[Diagnostic] = []
    failures: list[FailureReport] = []
    seen_ids: set[str] = set()
    for v in vs:
        loc = Location(file=v.file, line=v.line) if v.file else None
        diagnostics.append(Diagnostic(tool=TOOL, rule_id=v.rule, severity="error", message=v.message, location=loc))
        base = f"{STAGE}:{v.rule}:{v.file or '-'}:{v.line or 0}"
        fid, n = base, 2
        while fid in seen_ids:
            fid, n = f"{base}#{n}", n + 1
        seen_ids.add(fid)
        failures.append(
            FailureReport(
                id=fid,
                kind="policy",
                tool=TOOL,
                property_id=v.rule,
                description=v.message,
                location=loc,
                explanation=RULES.get(v.rule, ""),
                raw_ref=raw_refs.get((v.file or "", "nm")) if v.rule in ("SYMBOL_OVERRIDE", "DANGEROUS_IMPORT") else None,
            )
        )

    def result(outcome: Outcome, summary: str, status: Status | None) -> StageResult:
        return StageResult(
            stage=STAGE,
            outcome=outcome,
            status=status,
            summary=summary,
            diagnostics=diagnostics,
            failures=failures,
            invocations=invocations,
            details=details,
            duration_s=round(time.monotonic() - t0, 3),
        )

    if vs:
        rules = ", ".join(dict.fromkeys(v.rule for v in vs))
        incomplete = f"; some checks could not run: {'; '.join(problems + timeouts)}" if problems or timeouts else ""
        return result(
            Outcome.FAIL,
            f"{len(vs)} source-integrity violation(s) ({rules}): the code under test tampers with or diverges "
            f"between the verification tools{incomplete}",
            Status.STATIC_ANALYSIS_WARNING,
        )
    if timeouts:
        return result(Outcome.TIMEOUT, "; ".join(timeouts), outcome_to_status(STAGE, Outcome.TIMEOUT))
    if problems:
        return result(Outcome.ERROR, "source-integrity checks could not run: " + "; ".join(problems),
                      outcome_to_status(STAGE, Outcome.ERROR))
    checked = ", ".join(f"{f['source']} ({'/'.join(f['checks'])})" for f in details["files"])
    return result(Outcome.PASS, f"source integrity OK: {checked}", None)


# =========================================================================== the C++ gate
def _object_path(obj_root: Path, rel: str) -> Path:
    return obj_root / (rel[:-2] + ".o" if rel.endswith(".c") else rel + ".o")


def run_cpp_integrity(ctx) -> StageResult:
    """The integrity gate for C++ specs (see :mod:`leanc.rules.cpp_rules`); called by
    :func:`run_integrity`."""
    t0 = time.monotonic()
    spec = ctx.spec
    cxx = compiler(ctx)
    snap = Path(os.path.realpath(ctx.snapshot_dir))
    obj_root = ctx.build_dir / "obj"
    raw_root = ctx.raw_dir / STAGE
    raw_root.mkdir(parents=True, exist_ok=True)
    nm_tool = _find(("llvm-nm-18", "llvm-nm", "nm"))
    readelf = _find(("llvm-readelf-18", "llvm-readelf", "readelf"))
    demangler = _find(("llvm-cxxfilt-18", "llvm-cxxfilt", "c++filt"))
    details: dict = {
        "language": "c++",
        "tools": {"clang++": cxx, "nm": nm_tool, "readelf": readelf, "demangler": demangler},
        "rules": sorted(CPP_RULES),
        "allowed_imports": sorted(ALLOWED_IMPORTS),
        "allowed_cpp_runtime_imports": sorted(CPP_ALLOWED_RUNTIME_IMPORTS)
        + ["global operator new/delete", "std::nothrow", "std::__throw_*", "std::__glibcxx_assert_fail",
           "vtable for __cxxabiv1::__*class_type_info", "std::terminate/__cxa_begin_catch (from __clang_call_terminate)"],
        "files": [],
        "violations": 0,
    }
    invocations = []
    problems: list[str] = []
    timeouts: list[str] = []
    limits = Limits(timeout_s=ctx.timeout(60), max_output_bytes=16 * 1024 * 1024)
    violations: list[PolicyViolation] = []
    raw_refs: dict[tuple[str, str], str] = {}

    headers: dict[str, str] = {}
    for h in spec.sources.headers:
        p = snap / h
        if p.is_file():
            headers[h] = p.read_text(encoding="utf-8", errors="replace")
        else:
            problems.append(f"protected header {h} is missing from the snapshot")
    facts = header_facts(headers)

    def run(tool: str, argv: list[str], rel: str, what: str, stdin: bytes | None = None) -> str | None:
        proc = ctx.run(argv, limits=limits, cwd=snap, stdin_data=stdin)
        invocations.append(proc.to_invocation(tool, ctx.run_dir, ctx.log_name(STAGE, f"{what}-{rel}")))
        raw = raw_root / f"{what}-{rel.replace('/', '_')}.txt"
        raw.write_text(redact(proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else "")),
                       encoding="utf-8")
        raw_refs[(rel, what)] = ctx.relrun(raw)
        if proc.timed_out:
            timeouts.append(f"{what} of {rel} timed out")
            return None
        if not proc.ok or proc.output_truncated:
            reason = proc.error or ("output truncated" if proc.output_truncated else f"exit {proc.exit_code}")
            problems.append(f"{what} of {rel} failed ({reason}): {redact(proc.stderr.strip())[:300]}")
            return None
        return proc.stdout

    defines = [d if d.startswith("-D") else f"-D{d}" for d in spec.compile.defines]
    per_file: dict[str, dict] = {}
    for rel in spec.sources.editable:
        entry: dict = {"source": rel, "checks": [], "argv": {}}
        details["files"].append(entry)
        path = snap / rel
        if not path.is_file():
            problems.append(f"editable source {rel} is missing from the snapshot")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        src = CppSource(text)
        per_file[rel] = {"src": src, "entry": entry}
        violations += cpp_source_violations(spec, rel, text, headers=headers)
        entry["checks"].append("source")

        if cxx:
            argv = [cxx, f"-std={spec.language.standard}", *spec.compile.flags, *defines, *ctx.include_args(),
                    "-E", rel]
            entry["argv"]["preprocess"] = argv
            pp = run("clang++", argv, rel, "preprocess")
            if pp is not None:
                violations += cpp_preprocessed_violations(rel, pp, facts)
                entry["checks"].append("preprocessed")
            sargv = [cxx, f"-std={spec.language.standard}", *defines, *ctx.include_args(), "-fsyntax-only",
                     "-fno-color-diagnostics", "-fno-caret-diagnostics", "-Wno-everything", "-Wvla", "-Walloca",
                     "-Werror=vla", "-Werror=alloca", rel]
            entry["argv"]["stack"] = sargv
            proc = ctx.run(sargv, limits=limits, cwd=snap)
            invocations.append(proc.to_invocation("clang++", ctx.run_dir, ctx.log_name(STAGE, f"stack-{rel}")))
            if proc.timed_out:
                timeouts.append(f"stack check of {rel} timed out")
            elif proc.error:
                problems.append(f"stack check of {rel} failed: {proc.error}")
            else:
                hits = 0
                for m in _STACK_DIAG_RE.finditer(proc.stderr):
                    hits += 1
                    violations.append(_v("UNBOUNDED_STACK", f"{m.group('msg').strip()} (a stack allocation of "
                                            "input-dependent size)", rel, int(m.group("line"))))
                if proc.exit_code not in (0, None) and not hits:
                    problems.append(f"stack check of {rel} failed (exit {proc.exit_code}): "
                                    f"{redact(proc.stderr.strip())[:300]}")
                entry["checks"].append("stack")
        else:
            problems.append("clang++ not found: cannot check the preprocessed source")

        obj = _object_path(obj_root, rel)
        if not obj.is_file():
            problems.append(f"object file for {rel} not found ({obj}); the compile gate must run first")
            continue
        real_obj = os.path.realpath(obj)
        if nm_tool and demangler:
            argv = [nm_tool, "-P", real_obj]
            entry["argv"]["nm"] = argv
            out = run("nm", argv, rel, "nm")
            if out is not None:
                syms = parse_nm(out)
                names = [n for n, _ in syms]
                dem_out = run("c++filt", [demangler], rel, "demangle", ("\n".join(names) + "\n").encode()) \
                    if names else ""
                if dem_out is not None:
                    dem = dem_out.split("\n")[: len(names)] if names else []
                    if len(dem) != len(names) or any(not d.strip() for d in dem):
                        problems.append(f"demangling the symbols of {rel} produced unexpected output")
                    else:
                        triples = [(n, t, d.strip()) for (n, t), d in zip(syms, dem)]
                        per_file[rel]["symbols"] = triples
                        entry["defined"] = sorted(d for _, t, d in triples if t.isupper() and t != "U")
                        entry["undefined"] = sorted(d for _, t, d in triples if t in ("U", "w", "v"))
                        entry["checks"].append("symbols")
        else:
            problems.append("nm / llvm-nm or a C++ demangler (llvm-cxxfilt / c++filt) not found: cannot check "
                            "object symbols")
        if readelf:
            argv = [readelf, "-S", "-W", real_obj]
            entry["argv"]["readelf"] = argv
            out = run("readelf", argv, rel, "sections")
            if out is not None:
                entry["sections"] = [s for s in dict.fromkeys(_SECTION_RE.findall(out)) if s != "NULL"]
                violations += section_violations(rel, out, src)
                entry["checks"].append("sections")
        else:
            problems.append("readelf / llvm-readelf not found: cannot check object sections")

    all_defined = {
        rel: {n for n, t, _ in info.get("symbols", []) if t not in ("U", "w", "v")} for rel, info in per_file.items()
    }
    for rel, info in per_file.items():
        if "symbols" not in info:
            continue
        elsewhere = set().union(*(d for r, d in all_defined.items() if r != rel)) if len(all_defined) > 1 else set()
        violations += check_cpp_object_symbols(spec, rel, info["symbols"], facts=facts, defined_elsewhere=elsewhere,
                                               src=info["src"])
    if per_file and all("symbols" in i for i in per_file.values()) and not any(
        _defines_target(i["symbols"], spec.target.function) for i in per_file.values()
    ):
        violations.append(_v("MISSING_TARGET", f"no editable object defines the target function "
                                f"'{spec.target.function}' (as an external function or member function)", None, None))

    order = {r: k for k, r in enumerate(CPP_RULES)}
    uniq: dict[tuple, PolicyViolation] = {}
    for v in violations:
        uniq.setdefault((v.rule, v.file, v.line) if v.line else (v.rule, v.file, v.message), v)
    vs = sorted(uniq.values(), key=lambda v: (v.file or "", v.line or 0, order.get(v.rule, 99), v.message))
    details["violations"] = len(vs)
    details["problems"] = problems + timeouts
    diagnostics: list[Diagnostic] = []
    failures: list[FailureReport] = []
    seen_ids: set[str] = set()
    for v in vs:
        loc = Location(file=v.file, line=v.line) if v.file else None
        diagnostics.append(Diagnostic(tool=TOOL, rule_id=v.rule, severity="error", message=v.message, location=loc))
        base = f"{STAGE}:{v.rule}:{v.file or '-'}:{v.line or 0}"
        fid, n = base, 2
        while fid in seen_ids:
            fid, n = f"{base}#{n}", n + 1
        seen_ids.add(fid)
        failures.append(
            FailureReport(
                id=fid,
                kind="policy",
                tool=TOOL,
                property_id=v.rule,
                description=v.message,
                location=loc,
                explanation=CPP_RULES.get(v.rule, ""),
                raw_ref=raw_refs.get((v.file or "", "demangle")) or raw_refs.get((v.file or "", "nm"))
                if v.rule in ("SYMBOL_OVERRIDE", "DANGEROUS_IMPORT", "EXCEPTIONS", "CPP_RUNTIME", "TERMINATION")
                else None,
            )
        )

    def result(outcome: Outcome, summary: str, status: Status | None) -> StageResult:
        return StageResult(
            stage=STAGE,
            outcome=outcome,
            status=status,
            summary=summary,
            diagnostics=diagnostics,
            failures=failures,
            invocations=invocations,
            details=details,
            duration_s=round(time.monotonic() - t0, 3),
        )

    if vs:
        rules = ", ".join(dict.fromkeys(v.rule for v in vs))
        incomplete = f"; some checks could not run: {'; '.join(problems + timeouts)}" if problems or timeouts else ""
        return result(
            Outcome.FAIL,
            f"{len(vs)} source-integrity violation(s) ({rules}): the C++ code under test tampers with or diverges "
            f"between the verification tools{incomplete}",
            Status.STATIC_ANALYSIS_WARNING,
        )
    if timeouts:
        return result(Outcome.TIMEOUT, "; ".join(timeouts), outcome_to_status(STAGE, Outcome.TIMEOUT))
    if problems:
        return result(Outcome.ERROR, "source-integrity checks could not run: " + "; ".join(problems),
                      outcome_to_status(STAGE, Outcome.ERROR))
    checked = ", ".join(f"{f['source']} ({'/'.join(f['checks'])})" for f in details["files"])
    return result(Outcome.PASS, f"source integrity OK (C++): {checked}", None)
