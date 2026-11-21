from __future__ import annotations

from dataclasses import dataclass, field

from leanc.backends.sanitizer.ownership_log import (
    PHASE_AFTER,
    PHASE_IN,
    Event,
    ProbeKey,
)

MAX_PROBED_EXITS = 64
LOSS_INSIDE_FUNCTION = "inside-function"
LOSS_AT_FUNCTION_RETURN = "at-function-return"
LOSS_AFTER_RETURN_BY_CALLER = "after-return-by-caller"
LOSS_AT_OR_AFTER_RETURN = "at-or-after-return"


@dataclass
class FrameRecord:
    function: int
    call_site: int
    exit_ordinal: int | None = None


@dataclass
class Allocation:
    order: int
    pointer: int
    size: int
    site: int
    frames: tuple[int, ...]
    freed_site: int | None = None


@dataclass
class TraceRun:
    frames: list[FrameRecord] = field(default_factory=list)
    allocations: list[Allocation] = field(default_factory=list)
    live: dict[int, int] = field(default_factory=dict)
    signature: list[tuple[int, ...]] = field(default_factory=list)


@dataclass(frozen=True)
class OwnershipLossFinding:
    allocation: Allocation
    loss: str
    frame: FrameRecord


def _allocate(run: TraceRun, stack: list[int], pointer: int, size: int, site: int) -> None:
    run.allocations.append(Allocation(len(run.allocations), pointer, size, site, tuple(stack)))
    run.live[pointer] = len(run.allocations) - 1


def _release(run: TraceRun, pointer: int, site: int) -> None:
    index = run.live.pop(pointer, None)
    if index is not None:
        run.allocations[index].freed_site = site


def _apply(run: TraceRun, stack: list[int], event: Event) -> str | None:
    values = event.values
    if event.kind == "E":
        run.frames.append(FrameRecord(values[0], values[1]))
        stack.append(len(run.frames) - 1)
    elif event.kind == "X":
        if not stack or run.frames[stack[-1]].function != values[1]:
            return "function enter/exit events are unbalanced (longjmp, exit inside a call, or a forged log)"
        run.frames[stack.pop()].exit_ordinal = values[0]
    elif event.kind == "A":
        _allocate(run, stack, values[0], values[1], values[2])
    elif event.kind == "R":
        _release(run, values[0], values[3])
        if values[1]:
            _allocate(run, stack, values[1], values[2], values[3])
    elif event.kind == "F":
        _release(run, values[0], values[1])
    return None


def _signature_item(event: Event) -> tuple[int, ...] | None:
    if event.kind == "C":
        return None
    kind = ord(event.kind)
    if event.kind == "A":
        return (kind, event.values[1], event.values[2])
    if event.kind == "R":
        return (kind, int(event.values[0] != 0), int(event.values[1] != 0), event.values[2], event.values[3])
    if event.kind == "F":
        return (kind, event.values[1])
    return (kind, *event.values)


def build_trace(events: list[Event]) -> TraceRun | str:
    run = TraceRun()
    stack: list[int] = []
    for event in events:
        problem = _apply(run, stack, event)
        if problem:
            return problem
        item = _signature_item(event)
        if item is not None:
            run.signature.append(item)
    return run


def leaked_allocations(run: TraceRun, leaked_at_exit: frozenset[int]) -> list[Allocation]:
    return sorted(
        (run.allocations[i] for pointer, i in run.live.items() if pointer in leaked_at_exit and run.allocations[i].frames),
        key=lambda a: a.order,
    )


def candidate_exits(run: TraceRun, allocation: Allocation) -> list[int]:
    ordinals = []
    for frame_index in reversed(allocation.frames):
        ordinal = run.frames[frame_index].exit_ordinal
        if ordinal is None:
            break
        ordinals.append(ordinal)
    return ordinals


def probe_plan(run: TraceRun, leaked: list[Allocation]) -> list[int]:
    wanted = sorted({o for allocation in leaked for o in candidate_exits(run, allocation)})
    return wanted[:MAX_PROBED_EXITS]


def _classify(
    run: TraceRun, allocation: Allocation, probes: dict[ProbeKey, frozenset[int]]
) -> OwnershipLossFinding | None:
    frame_indexes = list(reversed(allocation.frames))
    ordinals = candidate_exits(run, allocation)
    if not ordinals or len(ordinals) != len(frame_indexes):
        return None
    for frame_index, ordinal in zip(frame_indexes, ordinals):
        frame = run.frames[frame_index]
        inside = probes.get(ProbeKey(ordinal, PHASE_IN))
        if inside is None:
            return None
        if allocation.pointer in inside:
            return OwnershipLossFinding(allocation, LOSS_INSIDE_FUNCTION, frame)
        after = probes.get(ProbeKey(ordinal, PHASE_AFTER))
        if after is None:
            return OwnershipLossFinding(allocation, LOSS_AT_OR_AFTER_RETURN, frame)
        if allocation.pointer in after:
            return OwnershipLossFinding(allocation, LOSS_AT_FUNCTION_RETURN, frame)
    return OwnershipLossFinding(allocation, LOSS_AFTER_RETURN_BY_CALLER, run.frames[frame_indexes[-1]])


def classify_losses(
    record: TraceRun, probe: TraceRun, leaked: list[Allocation], probes: dict[ProbeKey, frozenset[int]]
) -> list[OwnershipLossFinding] | str:
    if record.signature != probe.signature:
        return "the probe replay diverged from the recording run"
    findings = []
    for allocation in leaked:
        replayed = probe.allocations[allocation.order]
        if replayed.pointer != allocation.pointer:
            return "the probe replay allocated at different addresses"
        finding = _classify(probe, replayed, probes)
        if finding is not None:
            findings.append(finding)
    return findings


def release_sites(runs: list[TraceRun], allocation_site: int) -> list[int]:
    return sorted(
        {a.freed_site for run in runs for a in run.allocations if a.site == allocation_site and a.freed_site is not None}
    )
