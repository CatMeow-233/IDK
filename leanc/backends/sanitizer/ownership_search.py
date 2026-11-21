from __future__ import annotations

from dataclasses import dataclass, field

from leanc.backends.sanitizer.ownership_analysis import (
    LOST_DURING_CALL,
    NOT_LOCALISED,
    UNREACHABLE_BY_NEXT_EVENT,
    UNREACHABLE_BY_RETURN,
    Allocation,
    OwnershipLossFinding,
    TraceRun,
    call_chain,
    is_inlined,
)
from leanc.backends.sanitizer.ownership_log import PHASE_AFTER, PHASE_IN, ProbeKey

MAX_SEARCH_ROUNDS = 3
MAX_PROBED_EXITS = 64
PHASES_IN_TIME_ORDER = (PHASE_IN, PHASE_AFTER)


@dataclass
class LossSearch:
    allocation: Allocation
    candidates: list[int]
    low: int = 0
    high: int = -1
    found: ProbeKey | None = None
    sampled: set[int] = field(default_factory=set)

    @property
    def open(self) -> bool:
        return self.low <= self.high

    def sample(self, count: int) -> list[int]:
        span = self.high - self.low + 1
        if span <= count:
            indexes = range(self.low, self.high + 1)
        else:
            indexes = sorted({self.low + (k + 1) * span // count - 1 for k in range(count)})
        return [self.candidates[i] for i in indexes]


def start_searches(run: TraceRun, allocations: list[Allocation]) -> list[LossSearch]:
    ordered = [ordinal for ordinal, _ in sorted(run.exit_positions.items(), key=lambda item: item[1])]
    searches = []
    for allocation in allocations:
        candidates = [o for o in ordered if run.exit_positions[o] > allocation.position]
        searches.append(LossSearch(allocation, candidates, high=len(candidates) - 1))
    return searches


def next_probes(searches: list[LossSearch]) -> list[int]:
    pending = [s for s in searches if s.open]
    if not pending:
        return []
    share = max(1, MAX_PROBED_EXITS // len(pending))
    wanted: list[int] = []
    for search in pending:
        for ordinal in search.sample(share):
            if ordinal not in wanted and len(wanted) < MAX_PROBED_EXITS:
                wanted.append(ordinal)
    return sorted(wanted)


def _first_listed(search: LossSearch, probed: set[int], probes: dict[ProbeKey, frozenset[int]]) -> tuple[int, ProbeKey] | None:
    for index in range(search.low, search.high + 1):
        ordinal = search.candidates[index]
        if ordinal not in probed:
            continue
        for phase in PHASES_IN_TIME_ORDER:
            key = ProbeKey(ordinal, phase)
            if search.allocation.pointer in probes.get(key, frozenset()):
                return index, key
    return None


def narrow(search: LossSearch, probed: set[int], probes: dict[ProbeKey, frozenset[int]]) -> None:
    visible = [i for i in range(search.low, search.high + 1) if search.candidates[i] in probed]
    if not visible:
        search.low = search.high + 1
        return
    listed = _first_listed(search, probed, probes)
    if listed is None:
        search.low = visible[-1] + 1
        return
    index, key = listed
    search.found = key
    earlier = [i for i in visible if i < index]
    search.low = earlier[-1] + 1 if earlier else search.low
    search.high = index - 1


def classify(run: TraceRun, search: LossSearch) -> OwnershipLossFinding:
    allocation = search.allocation
    chain = call_chain(run, allocation)
    key = search.found
    frame_index = run.exit_frames.get(key.exit_ordinal) if key else None
    if key is None or frame_index is None:
        return OwnershipLossFinding(allocation, chain, NOT_LOCALISED, None)
    frame = run.frames[frame_index]
    inlined = is_inlined(run, frame)
    if key.phase != PHASE_IN:
        return OwnershipLossFinding(allocation, chain, UNREACHABLE_BY_NEXT_EVENT, frame, inlined)
    loss = LOST_DURING_CALL if frame_index in allocation.frames else UNREACHABLE_BY_RETURN
    return OwnershipLossFinding(allocation, chain, loss, frame, inlined)


def replay_matches(record: TraceRun, probe: TraceRun, allocations: list[Allocation]) -> str | None:
    if record.signature != probe.signature:
        return "the probe replay diverged from the recording run"
    if any(probe.allocations[a.order].pointer != a.pointer for a in allocations):
        return "the probe replay allocated at different addresses"
    return None
