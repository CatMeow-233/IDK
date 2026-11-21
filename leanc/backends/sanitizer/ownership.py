from __future__ import annotations

import os
import shutil
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from leanc.backends.sanitizer.ownership_analysis import (
    OwnershipLossFinding,
    TraceRun,
    build_trace,
    classify_losses,
    leaked_allocations,
    probe_plan,
    release_sites,
)
from leanc.backends.sanitizer.ownership_build import STAGE, build_trace_binary
from leanc.backends.sanitizer.ownership_log import (
    ProbeKey,
    leaked_objects_at_exit,
    leaked_objects_per_probe,
    parse_events,
)
from leanc.backends.sanitizer.ownership_symbols import (
    CodeAddress,
    Symbolized,
    symbolize,
)
from leanc.backends.sanitizer.plan import SanitizerPlan, find_llvm_symbolizer
from leanc.backends.sanitizer.runtime_reports import MEMORY_LEAK_RULE
from leanc.foundation.models import FailureReport, MemoryOwnershipTrace, ToolInvocation
from leanc.foundation.redact import redact
from leanc.foundation.runner import Limits
from leanc.foundation.sandbox import SandboxMounts
from leanc.foundation.text import safe_name
from leanc.specs.model import TestCase
from leanc.toolchain.context import BackendContext
from leanc.toolchain.cxx import compiler, compiler_version

RUN_TOOL = "ownership-trace-run"
OUT_DIR_NAME = "ownership"
LOG_FILE = "ownership.log"
LOG_PATH_ENV = "LEANC_OWNERSHIP_LOG"
PROBED_EXITS_ENV = "LEANC_OWNERSHIP_CHECKS"
LSAN_OPTIONS = "LSAN_OPTIONS"
REPORT_OBJECTS_OPTION = "report_objects=1"
MAX_LOG_BYTES = 8 * 1024 * 1024
BUDGET_S = 120.0
TEST_CASE_INPUT = "test_case"
FAILURE_ID_PREFIX = "sanitizer:"
LEAK_TOOL = "lsan"
MAX_RELEASE_SITES = 5
DETAILS_KEY = "ownership_trace"


@dataclass
class OwnershipInputs:
    ctx: BackendContext
    plan: SanitizerPlan
    env: dict[str, str]
    project_files: set[str]
    case_limits: Callable[[TestCase], Limits]


@dataclass
class _Replay:
    run: TraceRun | None = None
    leaked_at_exit: frozenset[int] = frozenset()
    probes: dict[ProbeKey, frozenset[int]] = field(default_factory=dict)
    problem: str | None = None


@dataclass
class _Session:
    inputs: OwnershipInputs
    out_root: Path
    raw_root: Path
    binary: Path
    symbolizer: str
    invocations: list[ToolInvocation]
    deadline: float


def _case_name(failure: FailureReport) -> str | None:
    return next((i.value for i in failure.inputs if i.name == TEST_CASE_INPUT), None)


def leak_failures_by_case(failures: list[FailureReport]) -> dict[str, list[FailureReport]]:
    by_case: dict[str, list[FailureReport]] = {}
    for f in failures:
        name = _case_name(f)
        if f.tool == LEAK_TOOL and f.property_id == MEMORY_LEAK_RULE and f.id.startswith(FAILURE_ID_PREFIX) and name:
            by_case.setdefault(name, []).append(f)
    return by_case


@dataclass(frozen=True)
class _LogRead:
    text: str = ""
    problem: str | None = None


def _read_regular_file(fd: int) -> _LogRead:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        return _LogRead(problem=f"{LOG_FILE} is not a regular file")
    chunks: list[bytes] = []
    size = 0
    while size <= MAX_LOG_BYTES:
        chunk = os.read(fd, MAX_LOG_BYTES + 1 - size)
        if not chunk:
            return _LogRead(text=b"".join(chunks).decode("utf-8", errors="replace"))
        chunks.append(chunk)
        size += len(chunk)
    return _LogRead(problem=f"{LOG_FILE} exceeds {MAX_LOG_BYTES} bytes")


def _read_trace_log(path: Path) -> _LogRead:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        return _LogRead(problem=f"cannot open {LOG_FILE} ({exc.strerror})")
    try:
        return _read_regular_file(fd)
    except OSError as exc:
        return _LogRead(problem=f"cannot read {LOG_FILE} ({exc.strerror})")
    finally:
        os.close(fd)


def _replay_env(inputs: OwnershipInputs, log_path: Path, probed: list[int] | None) -> dict[str, str]:
    env = dict(inputs.env)
    env[LSAN_OPTIONS] = f"{env[LSAN_OPTIONS]}:{REPORT_OBJECTS_OPTION}" if env.get(LSAN_OPTIONS) else REPORT_OBJECTS_OPTION
    env[LOG_PATH_ENV] = str(log_path)
    if probed is not None:
        env[PROBED_EXITS_ENV] = ",".join(str(o) for o in probed)
    return env


def _run_program(session: _Session, index: int, case: TestCase, probed: list[int] | None):
    ctx = session.inputs.ctx
    stem = f"{index:02d}-{safe_name(case.name, 60) or 'case'}-{'probe' if probed is not None else 'record'}"
    case_dir = session.out_root / "cases" / stem
    case_dir.mkdir(parents=True)
    log_path = Path(os.path.realpath(case_dir)) / LOG_FILE
    proc = ctx.run(
        [str(session.binary), *case.args],
        limits=session.inputs.case_limits(case),
        cwd=case_dir,
        env_extra=_replay_env(session.inputs, log_path, probed),
        stdin_data=case.stdin.encode("utf-8") if case.stdin is not None else None,
        mounts=SandboxMounts(ro=[str(session.out_root)], rw=[str(case_dir)]),
    )
    session.invocations.append(proc.to_invocation(RUN_TOOL, ctx.run_dir, ctx.log_name(STAGE, f"ownership {stem}")))
    (session.raw_root / f"{stem}.stderr.txt").write_text(redact(proc.stderr), encoding="utf-8")
    return proc, log_path, stem


def _replay(session: _Session, index: int, case: TestCase, probed: list[int] | None) -> _Replay:
    proc, log_path, stem = _run_program(session, index, case, probed)
    if proc.error or proc.timed_out or proc.output_truncated:
        return _Replay(problem=f"replay of {case.name!r} did not complete ({proc.error or 'timeout or output limit'})")
    log = _read_trace_log(log_path)
    if log.problem:
        return _Replay(problem=f"replay of {case.name!r}: {log.problem}")
    (session.raw_root / f"{stem}.log.txt").write_text(redact(log.text), encoding="utf-8")
    events = parse_events(log.text)
    if isinstance(events, str):
        return _Replay(problem=events)
    run = build_trace(events)
    if isinstance(run, str):
        return _Replay(problem=run)
    if probed is None:
        leaked = leaked_objects_at_exit(proc.stderr)
        return _Replay(problem=leaked) if isinstance(leaked, str) else _Replay(run=run, leaked_at_exit=leaked)
    probes = leaked_objects_per_probe(proc.stderr, events)
    return _Replay(problem=probes) if isinstance(probes, str) else _Replay(run=run, probes=probes)


def _unsupported(inputs: OwnershipInputs, symbolizer: str | None) -> str | None:
    if inputs.ctx.spec.language.is_cpp:
        return "C++ allocations (operator new/delete) bypass the traced malloc family"
    if not inputs.plan.detect_leaks:
        return "leak detection is disabled"
    if symbolizer is None:
        return "llvm-symbolizer not found under /usr"
    return None


def _addresses(finding: OwnershipLossFinding, runs: list[TraceRun]) -> list[CodeAddress]:
    sites = release_sites(runs, finding.allocation.site)[:MAX_RELEASE_SITES]
    return [
        CodeAddress(finding.allocation.site, True),
        CodeAddress(finding.frame.function, False),
        CodeAddress(finding.frame.call_site, True),
        *(CodeAddress(s, True) for s in sites),
    ]


def _trace_for(case: str, finding: OwnershipLossFinding, runs: list[TraceRun], symbols: Symbolized) -> MemoryOwnershipTrace | None:
    allocation = symbols.locations.get(CodeAddress(finding.allocation.site, True))
    function = symbols.functions.get(CodeAddress(finding.frame.function, False))
    if allocation is None or function is None:
        return None
    released = [
        loc
        for s in release_sites(runs, finding.allocation.site)[:MAX_RELEASE_SITES]
        if (loc := symbols.locations.get(CodeAddress(s, True))) is not None
    ]
    return MemoryOwnershipTrace(
        test_case=case,
        allocation=allocation,
        released_at=released,
        loss=finding.loss,
        function=function,
        returned_to=symbols.locations.get(CodeAddress(finding.frame.call_site, True)),
    )


def _same_site(trace: MemoryOwnershipTrace, failure: FailureReport) -> bool:
    location = failure.location
    return location is not None and (location.file, location.line) == (trace.allocation.file, trace.allocation.line)


def _attach(traces: list[MemoryOwnershipTrace], failures: list[FailureReport]) -> int:
    attached = 0
    for failure in failures:
        trace = next((t for t in traces if _same_site(t, failure)), None)
        if trace is not None and failure.ownership_trace is None:
            failure.ownership_trace = trace
            attached += 1
    return attached


def _loss_findings(session: _Session, index: int, case: TestCase, record: _Replay) -> list[OwnershipLossFinding] | str:
    leaked = leaked_allocations(record.run, record.leaked_at_exit)
    if not leaked:
        return "the recording replay did not reproduce a leak allocated in instrumented code"
    probe = _replay(session, index, case, probe_plan(record.run, leaked))
    if probe.problem:
        return probe.problem
    return classify_losses(record.run, probe.run, leaked, probe.probes)


def _trace_cases(session: _Session, targets: dict[str, list[FailureReport]], per_case: dict[str, str]) -> str | None:
    cases = session.inputs.ctx.spec.tests.cases
    records: dict[str, _Replay] = {}
    budget_note = None
    for index, case in enumerate(cases):
        if time.monotonic() > session.deadline:
            budget_note = f"time budget exhausted after {len(records)} of {len(cases)} recording replays"
            break
        records[case.name] = _replay(session, index, case, None)
    runs = [r.run for r in records.values() if r.run is not None]
    for index, case in enumerate(cases):
        record = records.get(case.name)
        if case.name not in targets or record is None:
            continue
        if record.problem or time.monotonic() > session.deadline:
            per_case[case.name] = record.problem or "time budget exhausted"
            continue
        findings = _loss_findings(session, index, case, record)
        if isinstance(findings, str):
            per_case[case.name] = findings
            continue
        addresses = [a for f in findings for a in _addresses(f, runs)]
        symbols = symbolize(session.inputs.ctx, session.symbolizer, session.binary, addresses, session.inputs.project_files)
        session.invocations.extend(symbols.invocations)
        if symbols.problem:
            per_case[case.name] = symbols.problem
            continue
        traces = [t for f in findings if (t := _trace_for(case.name, f, runs, symbols)) is not None]
        per_case[case.name] = f"attached to {_attach(traces, targets[case.name])} leak failure(s)"
    return budget_note


def attach_ownership_traces(
    inputs: OwnershipInputs, failures: list[FailureReport], invocations: list[ToolInvocation], details: dict
) -> None:
    targets = leak_failures_by_case(failures)
    if not targets:
        return
    per_case: dict[str, str] = {}
    report: dict[str, object] = {"status": "", "cases": per_case}
    details[DETAILS_KEY] = report
    symbolizer = find_llvm_symbolizer(compiler(inputs.ctx), compiler_version(inputs.ctx))
    unsupported = _unsupported(inputs, symbolizer)
    if unsupported or symbolizer is None:
        report["status"] = f"not traced: {unsupported}"
        return
    ctx = inputs.ctx
    out_root = Path(os.path.realpath(ctx.build_dir)) / OUT_DIR_NAME
    raw_root = Path(os.path.realpath(ctx.raw_dir)) / STAGE / OUT_DIR_NAME
    if raw_root.exists():
        shutil.rmtree(raw_root)
    raw_root.mkdir(parents=True)
    build = build_trace_binary(ctx, inputs.plan, out_root)
    invocations.extend(build.invocations)
    if build.binary is None:
        report["status"] = f"not traced: {build.problem}"
        return
    deadline = time.monotonic() + ctx.timeout(BUDGET_S)
    session = _Session(inputs, out_root, raw_root, build.binary, symbolizer, invocations, deadline)
    budget_note = _trace_cases(session, targets, per_case)
    report["status"] = "traced (evidence for the repair model, never part of the verdict)"
    if budget_note:
        report["budget"] = budget_note
