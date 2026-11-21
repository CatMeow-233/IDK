from __future__ import annotations

import re
from dataclasses import dataclass

from leanc.foundation.models import FailureReport, MemoryOwnershipTrace

CALL_STACK_FRAME_RE = re.compile(r"^#\d+ .+ (?P<file>[^\s:]+):(?P<line>\d+)(?::\d+)?$")
SourceLine = tuple[str, int]


@dataclass(frozen=True)
class TraceCandidate:
    trace: MemoryOwnershipTrace
    chain: tuple[SourceLine, ...]


def failure_chain(failure: FailureReport, project_files: set[str]) -> tuple[SourceLine, ...]:
    lines = []
    for frame in failure.call_stack:
        match = CALL_STACK_FRAME_RE.match(frame)
        if match and match.group("file") in project_files:
            lines.append((match.group("file"), int(match.group("line"))))
    return tuple(lines)


def _matches(candidate: TraceCandidate, failure: FailureReport, chain: tuple[SourceLine, ...]) -> bool:
    location = failure.location
    if location is None or not candidate.chain or candidate.chain[0] != (location.file, location.line):
        return False
    return chain[: len(candidate.chain)] == candidate.chain


def _evidence_key(trace: MemoryOwnershipTrace) -> str:
    return trace.model_dump_json(exclude={"test_case"})


def attach_traces(candidates: list[TraceCandidate], failures: list[FailureReport], project_files: set[str]) -> list[str]:
    notes = []
    for failure in failures:
        chain = failure_chain(failure, project_files)
        matches = [c for c in candidates if _matches(c, failure, chain)]
        if not matches:
            notes.append(f"{failure.id}: no traced allocation matches its allocation stack")
            continue
        if len({_evidence_key(c.trace) for c in matches}) > 1:
            notes.append(f"{failure.id}: {len(matches)} traced allocations match with conflicting evidence; none attached")
            continue
        failure.ownership_trace = matches[0].trace
        notes.append(f"{failure.id}: attached")
    return notes
