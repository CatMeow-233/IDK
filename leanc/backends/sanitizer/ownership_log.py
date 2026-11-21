from __future__ import annotations

import re
from dataclasses import dataclass

MAX_EVENTS = 1_000_000
MAX_CHECKS = 256
MAX_LEAKED_OBJECTS = 4096
HEX = r"0x[0-9a-f]{1,16}"
DECIMAL = r"\d{1,19}"
CHECK_MARKER = "LEANC-OWNERSHIP-CHECK"
PHASE_IN = "in"
PHASE_AFTER = "after"

_EVENT_RES = {
    "A": re.compile(rf"^A ({HEX}) ({DECIMAL}) ({HEX})$"),
    "R": re.compile(rf"^R ({HEX}) ({HEX}) ({DECIMAL}) ({HEX})$"),
    "F": re.compile(rf"^F ({HEX}) ({HEX})$"),
    "E": re.compile(rf"^E ({HEX}) ({HEX})$"),
    "X": re.compile(rf"^X ({DECIMAL}) ({HEX}) ({HEX})$"),
    "T": re.compile(r"^T$"),
}
_CHECK_RE = re.compile(rf"^C ({DECIMAL}) ({PHASE_IN}|{PHASE_AFTER}) ([01])$")
_CHECK_MARKER_RE = re.compile(rf"^{CHECK_MARKER} ({DECIMAL}) ({PHASE_IN}|{PHASE_AFTER})$")
_LEAKED_OBJECT_RE = re.compile(rf"^({HEX}) \({DECIMAL} bytes\)$")
_LEAK_HEADER_RE = re.compile(r"^(Direct|Indirect) leak of ")
DIRECT = "Direct"
_REPORT_END_RE = re.compile(r"^SUMMARY: \w+Sanitizer: .*leaked in")


@dataclass(frozen=True)
class ProbeKey:
    exit_ordinal: int
    phase: str


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    values: tuple[int, ...]
    probe: ProbeKey | None = None
    leaked: bool = False


def _number(text: str) -> int:
    return int(text, 16) if text.startswith("0x") else int(text)


def _event(line: str) -> Event | None:
    check = _CHECK_RE.match(line)
    if check:
        return Event("C", (), ProbeKey(int(check.group(1)), check.group(2)), check.group(3) == "1")
    pattern = _EVENT_RES.get(line[:1])
    found = pattern.match(line) if pattern else None
    return Event(line[0], tuple(_number(g) for g in found.groups())) if found else None


def parse_events(text: str) -> list[Event] | str:
    events: list[Event] = []
    for number, line in enumerate(text.splitlines(), 1):
        event = _event(line)
        if event is None:
            return f"line {number} of the ownership log is not a trace event"
        events.append(event)
        if len(events) > MAX_EVENTS:
            return f"the ownership log has more than {MAX_EVENTS} events"
    if sum(1 for e in events if e.kind == "C") > MAX_CHECKS:
        return f"the ownership log has more than {MAX_CHECKS} probes"
    return events


@dataclass(frozen=True)
class LeakedObjects:
    direct: frozenset[int] = frozenset()
    indirect: frozenset[int] = frozenset()


def _objects(lines: list[str]) -> LeakedObjects | str:
    direct: set[int] = set()
    indirect: set[int] = set()
    kind = None
    for line in lines:
        header = _LEAK_HEADER_RE.match(line)
        if header:
            kind = header.group(1)
            continue
        found = _LEAKED_OBJECT_RE.match(line)
        if found and kind is not None:
            (direct if kind == DIRECT else indirect).add(int(found.group(1), 16))
    if len(direct) + len(indirect) > MAX_LEAKED_OBJECTS:
        return f"more than {MAX_LEAKED_OBJECTS} leaked objects reported"
    return LeakedObjects(frozenset(direct), frozenset(indirect))


def leaked_objects_at_exit(stderr: str) -> LeakedObjects | str:
    lines = stderr.splitlines()
    if any(_CHECK_MARKER_RE.match(line) for line in lines):
        return "a recording run printed probe markers"
    return _objects(lines)


def _segments(stderr: str) -> list[tuple[ProbeKey, list[str]]]:
    segments: list[tuple[ProbeKey, list[str]]] = []
    for line in stderr.splitlines():
        marker = _CHECK_MARKER_RE.match(line)
        if marker:
            segments.append((ProbeKey(int(marker.group(1)), marker.group(2)), []))
        elif segments:
            segments[-1][1].append(line)
    return segments


def _first_report(lines: list[str]) -> list[str] | None:
    for index, line in enumerate(lines):
        if _REPORT_END_RE.match(line):
            return lines[: index + 1]
    return None


def leaked_objects_per_probe(stderr: str, events: list[Event]) -> dict[ProbeKey, frozenset[int]] | str:
    checks = [e for e in events if e.kind == "C"]
    segments = _segments(stderr)
    if [c.probe for c in checks] != [key for key, _ in segments]:
        return "probe markers on stderr do not match the probes in the ownership log"
    leaked: dict[ProbeKey, frozenset[int]] = {}
    for check, (key, lines) in zip(checks, segments):
        if key in leaked:
            return f"probe {key.exit_ordinal} {key.phase} ran twice"
        if not check.leaked:
            leaked[key] = frozenset()
            continue
        report = _first_report(lines)
        if report is None:
            return f"probe {key.exit_ordinal} {key.phase} found leaks but printed no complete report"
        objects = _objects(report)
        if isinstance(objects, str):
            return objects
        leaked[key] = objects.direct
    return leaked
