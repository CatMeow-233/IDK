from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from leanc.backends.sanitizer.ownership_analysis import (
    Allocation,
    OwnershipLossFinding,
    TraceRun,
    leaked_allocations,
    release_summary,
)
from leanc.backends.sanitizer.ownership_attach import TraceCandidate, attach_traces
from leanc.backends.sanitizer.ownership_build import STAGE, build_trace_binary
from leanc.backends.sanitizer.ownership_replay import (
    OwnershipInputs,
    Replay,
    ReplaySession,
    replay,
)
from leanc.backends.sanitizer.ownership_search import (
    MAX_SEARCH_ROUNDS,
    classify,
    narrow,
    next_probes,
    replay_matches,
    start_searches,
)
from leanc.backends.sanitizer.ownership_symbols import (
    CodeAddress,
    Symbolized,
    symbolize,
)
from leanc.backends.sanitizer.plan import find_llvm_symbolizer
from leanc.backends.sanitizer.runtime_reports import MEMORY_LEAK_RULE
from leanc.foundation.models import (
    FailureReport,
    Location,
    MemoryOwnershipTrace,
    ToolInvocation,
)
from leanc.specs.model import TestCase
from leanc.toolchain.cxx import compiler, compiler_version

OUT_DIR_NAME = "ownership"
BUDGET_S = 120.0
TEST_CASE_INPUT = "test_case"
FAILURE_ID_PREFIX = "sanitizer:"
LEAK_TOOL = "lsan"
MAX_RELEASE_SITES = 5
DETAILS_KEY = "ownership_trace"
TRACED_STATUS = "traced (evidence for the repair model, never part of the verdict)"
BUDGET_EXHAUSTED = "time budget exhausted"
RECORDING_PROBLEMS_KEY = "recording_problems"

@dataclass
class _Session:
    replays: ReplaySession
    symbolizer: str
    deadline: float
    releases: dict[int, list[int]] = field(default_factory=dict)
    recording_problems: dict[str, str] = field(default_factory=dict)

    def out_of_time(self) -> bool:
        return time.monotonic() > self.deadline

    def add_releases(self, run: TraceRun) -> None:
        for site, frees in release_summary([run]).items():
            self.releases[site] = sorted(set(self.releases.get(site, [])) | set(frees))


def _case_name(failure: FailureReport) -> str | None:
    return next((i.value for i in failure.inputs if i.name == TEST_CASE_INPUT), None)


def leak_failures_by_case(failures: list[FailureReport]) -> dict[str, list[FailureReport]]:
    by_case: dict[str, list[FailureReport]] = {}
    for f in failures:
        name = _case_name(f)
        if f.tool == LEAK_TOOL and f.property_id == MEMORY_LEAK_RULE and f.id.startswith(FAILURE_ID_PREFIX) and name:
            by_case.setdefault(name, []).append(f)
    return by_case


def _unsupported(inputs: OwnershipInputs, symbolizer: str | None) -> str | None:
    if not sys.platform.startswith("linux"):
        return "ownership tracing uses Linux ELF linker wrapping (-Wl,--wrap) and is supported on Linux only"
    if inputs.ctx.spec.language.is_cpp:
        return "C++ allocations (operator new/delete) bypass the traced malloc family"
    if not inputs.plan.detect_leaks:
        return "leak detection is disabled"
    if symbolizer is None:
        return "llvm-symbolizer not found under /usr"
    return None


def _unique_findings(findings: list[OwnershipLossFinding]) -> list[OwnershipLossFinding]:
    seen: dict[tuple, OwnershipLossFinding] = {}
    for f in findings:
        frame_key = (f.frame.function, f.frame.call_site) if f.frame else None
        seen.setdefault((f.allocation.site, f.chain, f.loss, frame_key), f)
    return list(seen.values())


def _addresses(session: _Session, finding: OwnershipLossFinding) -> list[CodeAddress]:
    addresses = [CodeAddress(finding.allocation.site, True), *(CodeAddress(s, True) for s in finding.chain)]
    if finding.frame is not None:
        addresses += [CodeAddress(finding.frame.function, False), CodeAddress(finding.frame.call_site, True)]
    addresses += [CodeAddress(s, True) for s in session.releases.get(finding.allocation.site, [])[:MAX_RELEASE_SITES]]
    return addresses


def _source_line(location: Location | None) -> tuple[str, int] | None:
    return (location.file, location.line) if location and location.file and location.line else None


def _released_at(session: _Session, finding: OwnershipLossFinding, symbols: Symbolized) -> list[Location]:
    unique: dict[tuple, Location] = {}
    for site in session.releases.get(finding.allocation.site, [])[:MAX_RELEASE_SITES]:
        location = symbols.locations.get(CodeAddress(site, True))
        if location is not None:
            unique.setdefault((location.file, location.line, location.column, location.function), location)
    return list(unique.values())


def _candidate(session: _Session, case: str, finding: OwnershipLossFinding, symbols: Symbolized) -> TraceCandidate | None:
    allocation = symbols.locations.get(CodeAddress(finding.allocation.site, True))
    chain = [_source_line(symbols.locations.get(CodeAddress(s, True))) for s in finding.chain]
    function = symbols.functions.get(CodeAddress(finding.frame.function, False)) if finding.frame else None
    if allocation is None or any(line is None for line in chain) or (finding.frame is not None and function is None):
        return None
    returned_to = None
    if finding.frame is not None and not finding.inlined:
        returned_to = symbols.locations.get(CodeAddress(finding.frame.call_site, True))
    trace = MemoryOwnershipTrace(
        test_case=case,
        allocation=allocation,
        released_at=_released_at(session, finding, symbols),
        loss=finding.loss,
        function=function,
        returned_to=returned_to,
    )
    return TraceCandidate(trace, (_source_line(allocation), *chain))


@dataclass
class _CaseOutcome:
    findings: list[OwnershipLossFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


NOT_REPRODUCED = (
    "the instrumented replay showed no direct leak from this test case "
    "(instrumentation can keep stale references alive)"
)


def _search_losses(session: _Session, index: int, case: TestCase, record: TraceRun, allocations: list[Allocation]):
    searches = start_searches(record, allocations)
    for _ in range(MAX_SEARCH_ROUNDS):
        probed = next_probes(searches)
        if not probed or session.out_of_time():
            break
        probe = replay(session.replays, index, case, probed)
        problem = probe.problem or replay_matches(record, probe.run, allocations)
        if problem:
            return problem
        for search in searches:
            narrow(search, set(probed), probe.probes)
    return [classify(record, search) for search in searches]


def _loss_findings(session: _Session, index: int, case: TestCase, record: Replay) -> _CaseOutcome:
    selection = leaked_allocations(record.run, record.leaked_at_exit.direct, record.leaked_at_exit.indirect)
    outcome = _CaseOutcome()
    if selection.indirect_only:
        outcome.notes.append(f"{selection.indirect_only} indirect-only leak(s) not traced (reachable only from leaked blocks)")
    if selection.dropped:
        outcome.notes.append(f"{selection.dropped} further distinct leak(s) not traced (cap reached)")
    if not selection.allocations:
        outcome.notes.append(NOT_REPRODUCED)
        return outcome
    findings = _search_losses(session, index, case, record.run, selection.allocations)
    if isinstance(findings, str):
        outcome.notes.append(findings)
        return outcome
    outcome.findings = _unique_findings(findings)
    return outcome


def _trace_case(session: _Session, index: int, case: TestCase, record: Replay, failures: list[FailureReport]) -> list[str]:
    outcome = _loss_findings(session, index, case, record)
    if not outcome.findings or session.out_of_time():
        return outcome.notes + ([BUDGET_EXHAUSTED] if session.out_of_time() else [])
    addresses = [a for f in outcome.findings for a in _addresses(session, f)]
    inputs = session.replays.inputs
    symbols = symbolize(inputs.ctx, session.symbolizer, session.replays.binary, addresses, inputs.project_files)
    session.replays.invocations.extend(symbols.invocations)
    if symbols.problem:
        return outcome.notes + [symbols.problem]
    candidates = [c for f in outcome.findings if (c := _candidate(session, case.name, f, symbols)) is not None]
    return outcome.notes + attach_traces(candidates, failures, inputs.project_files)


def _record_all(session: _Session, cases: list[TestCase], targets: set[str]) -> tuple[dict[str, Replay], str | None]:
    kept: dict[str, Replay] = {}
    for index, case in enumerate(cases):
        if session.out_of_time():
            return kept, f"{BUDGET_EXHAUSTED} after {index} of {len(cases)} recording replays"
        record = replay(session.replays, index, case, None)
        if record.run is not None:
            session.add_releases(record.run)
        elif record.problem:
            session.recording_problems[case.name] = record.problem
        if case.name in targets:
            kept[case.name] = record
    return kept, None


def _trace_cases(session: _Session, targets: dict[str, list[FailureReport]], per_case: dict[str, list[str]]) -> str | None:
    cases = session.replays.inputs.ctx.spec.tests.cases
    records, budget_note = _record_all(session, cases, set(targets))
    for index, case in enumerate(cases):
        if case.name not in targets:
            continue
        record = records.get(case.name)
        if record is None:
            per_case[case.name] = [f"not recorded: {BUDGET_EXHAUSTED}"]
        elif record.problem:
            per_case[case.name] = [record.problem]
        elif session.out_of_time():
            per_case[case.name] = [BUDGET_EXHAUSTED]
        else:
            per_case[case.name] = _trace_case(session, index, case, record, targets[case.name])
    return budget_note


def attach_ownership_traces(
    inputs: OwnershipInputs, failures: list[FailureReport], invocations: list[ToolInvocation], details: dict
) -> None:
    targets = leak_failures_by_case(failures)
    if not targets:
        return
    per_case: dict[str, list[str]] = {}
    report: dict[str, object] = {"status": "", "cases": per_case}
    details[DETAILS_KEY] = report
    symbolizer = find_llvm_symbolizer(
        compiler(inputs.ctx), compiler_version(inputs.ctx), sandbox_active=inputs.ctx.sandbox.active
    )
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
    replays = ReplaySession(inputs, out_root, raw_root, build.binary, invocations)
    session = _Session(replays, symbolizer, time.monotonic() + ctx.timeout(BUDGET_S))
    budget_note = _trace_cases(session, targets, per_case)
    attached = sum(1 for f in failures if f.ownership_trace is not None)
    report["status"] = f"{TRACED_STATUS}; evidence attached to {attached} of {sum(map(len, targets.values()))} leak failure(s)"
    if budget_note:
        report["budget"] = budget_note
    if session.recording_problems:
        report[RECORDING_PROBLEMS_KEY] = dict(session.recording_problems)
