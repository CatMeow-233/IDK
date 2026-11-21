from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from leanc.backends.sanitizer.ownership_log import Event, ProbeKey

MAX_TRACED_ALLOCATIONS = 16
THREAD_EVENT = "T"
OUTSIDE_IMAGE = 0
LOST_DURING_CALL = "lost-during-call"
UNREACHABLE_BY_RETURN = "unreachable-by-return"
UNREACHABLE_BY_NEXT_EVENT = "unreachable-by-next-event"
NOT_LOCALISED = "not-localised"


@dataclass(slots=True)
class FrameRecord:
    function: int
    call_site: int
    parent: int | None
    exit_ordinal: int | None = None


@dataclass(slots=True)
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
    digest: Any = field(default_factory=hashlib.sha256)

    @property
    def signature(self) -> str:
        return self.digest.hexdigest()


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
    elif event.kind == THREAD_EVENT:
        return "the test program called traced code from several threads; ownership tracing supports single-threaded programs only"
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
            run.digest.update(repr(item).encode("ascii"))
    return run


def call_chain(run: TraceRun, allocation: Allocation) -> tuple[int, ...]:
    chain = []
    for index in reversed(allocation.frames):
        frame = run.frames[index]
        if frame.call_site == OUTSIDE_IMAGE:
            break
        if not is_inlined(run, frame):
            chain.append(frame.call_site)
    return tuple(chain)


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
        if pointer not in direct:
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


def is_inlined(run: TraceRun, frame: FrameRecord) -> bool:
    return frame.parent is not None and run.frames[frame.parent].call_site == frame.call_site


def release_summary(runs: list[TraceRun]) -> dict[int, list[int]]:
    released: dict[int, set[int]] = defaultdict(set)
    for run in runs:
        for allocation in run.allocations:
            if allocation.freed_site is not None:
                released[allocation.site].add(allocation.freed_site)
    return {site: sorted(sites) for site, sites in released.items()}
