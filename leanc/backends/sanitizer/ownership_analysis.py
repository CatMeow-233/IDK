from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from leanc.backends.sanitizer.ownership_log import PHASE_IN, Event, ProbeKey

MAX_PROBED_EXITS = 64
PROBES_PER_ALLOCATION = 8
MAX_TRACED_ALLOCATIONS = 16
LOST_DURING_CALL = "lost-during-call"
UNREACHABLE_BY_RETURN = "unreachable-by-return"
UNREACHABLE_BY_NEXT_EVENT = "unreachable-by-next-event"
NOT_LOCALISED = "not-localised"


@dataclass
class FrameRecord:
    function: int
    call_site: int
    parent: int | None
    exit_ordinal: int | None = None


@dataclass
class Allocation:
    order: int
    position: int
    pointer: int
    size: int
    site: int
    frames: tuple[int, ...]
    freed_site: int | None = None
    moved: bool = False


@dataclass
class TraceRun:
    frames: list[FrameRecord] = field(default_factory=list)
    allocations: list[Allocation] = field(default_factory=list)
    live: dict[int, int] = field(default_factory=dict)
    exit_positions: dict[int, int] = field(default_factory=dict)
    exit_frames: dict[int, int] = field(default_factory=dict)
    probe_positions: dict[ProbeKey, int] = field(default_factory=dict)
    signature: list[tuple[int, ...]] = field(default_factory=list)


@dataclass(frozen=True)
class OwnershipLossFinding:
    allocation: Allocation
    chain: tuple[int, ...]
    loss: str
    frame: FrameRecord | None
    inlined: bool = False


def _allocate(run: TraceRun, stack: list[int], position: int, pointer: int, size: int, site: int) -> None:
    run.allocations.append(Allocation(len(run.allocations), position, pointer, size, site, tuple(stack)))
    run.live[pointer] = len(run.allocations) - 1


def _free(run: TraceRun, pointer: int, site: int) -> None:
    index = run.live.pop(pointer, None)
    if index is not None:
        run.allocations[index].freed_site = site


def _reallocate(run: TraceRun, stack: list[int], position: int, values: tuple[int, ...]) -> None:
    old, new, size, site = values
    if new == 0 and size > 0:
        return
    if new == 0:
        _free(run, old, site)
        return
    index = run.live.pop(old, None)
    if index is not None:
        run.allocations[index].moved = True
    _allocate(run, stack, position, new, size, site)


def _apply(run: TraceRun, stack: list[int], position: int, event: Event) -> str | None:
    values = event.values
    if event.kind == "E":
        run.frames.append(FrameRecord(values[0], values[1], stack[-1] if stack else None))
        stack.append(len(run.frames) - 1)
    elif event.kind == "X":
        if not stack or run.frames[stack[-1]].function != values[1]:
            return "function enter/exit events are unbalanced (longjmp, exit inside a call, or a forged log)"
        frame_index = stack.pop()
        run.frames[frame_index].exit_ordinal = values[0]
        run.exit_positions[values[0]] = position
        run.exit_frames[values[0]] = frame_index
    elif event.kind == "A":
        _allocate(run, stack, position, values[0], values[1], values[2])
    elif event.kind == "R":
        _reallocate(run, stack, position, values)
    elif event.kind == "F":
        _free(run, values[0], values[1])
    elif event.probe is not None:
        run.probe_positions[event.probe] = position
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
    for position, event in enumerate(events):
        problem = _apply(run, stack, position, event)
        if problem:
            return problem
        item = _signature_item(event)
        if item is not None:
            run.signature.append(item)
    return run


def call_chain(run: TraceRun, allocation: Allocation) -> tuple[int, ...]:
    return tuple(run.frames[i].call_site for i in reversed(allocation.frames))


@dataclass
class LeakSelection:
    allocations: list[Allocation] = field(default_factory=list)
    indirect_only: int = 0
    dropped: int = 0


def leaked_allocations(run: TraceRun, direct: frozenset[int], indirect: frozenset[int] = frozenset()) -> LeakSelection:
    selection = LeakSelection()
    seen: set[tuple[int, tuple[int, ...]]] = set()
    for pointer, index in sorted(run.live.items(), key=lambda item: item[1]):
        allocation = run.allocations[index]
        if pointer in indirect and pointer not in direct:
            selection.indirect_only += 1
            continue
        if pointer not in direct or not allocation.frames:
            continue
        key = (allocation.site, call_chain(run, allocation))
        if key in seen:
            continue
        seen.add(key)
        if len(selection.allocations) >= MAX_TRACED_ALLOCATIONS:
            selection.dropped += 1
            continue
        selection.allocations.append(allocation)
    return selection


def probe_plan(run: TraceRun, leaked: list[Allocation]) -> list[int]:
    exits = sorted(run.exit_positions.items(), key=lambda item: item[1])
    wanted: set[int] = set()
    for allocation in leaked:
        after = [ordinal for ordinal, position in exits if position > allocation.position]
        wanted.update(after[:PROBES_PER_ALLOCATION])
    return sorted(wanted)[:MAX_PROBED_EXITS]


def _first_listing(run: TraceRun, allocation: Allocation, probes: dict[ProbeKey, frozenset[int]]) -> ProbeKey | None:
    ordered = sorted((pos, key) for key, pos in run.probe_positions.items() if pos > allocation.position)
    return next((key for _, key in ordered if allocation.pointer in probes.get(key, frozenset())), None)


def _is_inlined(run: TraceRun, frame: FrameRecord) -> bool:
    return frame.parent is not None and run.frames[frame.parent].call_site == frame.call_site


def _classify(run: TraceRun, allocation: Allocation, probes: dict[ProbeKey, frozenset[int]]) -> OwnershipLossFinding:
    chain = call_chain(run, allocation)
    key = _first_listing(run, allocation, probes)
    frame_index = run.exit_frames.get(key.exit_ordinal) if key else None
    if key is None or frame_index is None:
        return OwnershipLossFinding(allocation, chain, NOT_LOCALISED, None)
    frame = run.frames[frame_index]
    inlined = _is_inlined(run, frame)
    if key.phase != PHASE_IN:
        return OwnershipLossFinding(allocation, chain, UNREACHABLE_BY_NEXT_EVENT, frame, inlined)
    loss = LOST_DURING_CALL if frame_index in allocation.frames else UNREACHABLE_BY_RETURN
    return OwnershipLossFinding(allocation, chain, loss, frame, inlined)


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
        findings.append(_classify(probe, replayed, probes))
    return findings


def release_summary(runs: list[TraceRun]) -> dict[int, list[int]]:
    released: dict[int, set[int]] = defaultdict(set)
    for run in runs:
        for allocation in run.allocations:
            if allocation.freed_site is not None:
                released[allocation.site].add(allocation.freed_site)
    return {site: sorted(sites) for site, sites in released.items()}
