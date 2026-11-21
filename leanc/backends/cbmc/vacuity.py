from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from leanc.backends.cbmc.execution import TOOL, CbmcRun, ToolProblem, messages_excerpt
from leanc.backends.cbmc.harness import probe_occurrences
from leanc.backends.cbmc.output import PROPERTY_HOLDS, PROPERTY_VIOLATED
from leanc.backends.counterexample.locations import location_from_cbmc
from leanc.backends.counterexample.properties import REACH_PREFIX, VACUITY_ID_PREFIX, is_reach_probe
from leanc.backends.counterexample.reports import vacuity_failure
from leanc.foundation.models import FailureReport, Location
from leanc.foundation.status import Outcome

PROBE_REACHED = PROPERTY_VIOLATED
PROBE_UNREACHABLE = PROPERTY_HOLDS
PROBE_DECIDED = (PROBE_REACHED, PROBE_UNREACHABLE)

class ProbeProperty(NamedTuple):
    property_id: str
    label: str
    status: str
    result: dict


@dataclass
class ProbeVerdict:
    details: dict
    failures: list[FailureReport] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    inconclusive: str = ""
    problem: ToolProblem | None = None


def _reported_probes(run: CbmcRun) -> list[ProbeProperty]:
    probes = []
    for index, result in enumerate(run.output.results or []):
        if not is_reach_probe(result):
            continue
        probes.append(
            ProbeProperty(
                property_id=str(result.get("property") or f"<probe {index}>"),
                label=str(result.get("description"))[len(REACH_PREFIX) :],
                status=str(result.get("status", "")).upper(),
                result=result,
            )
        )
    return probes


def _probe_location(result: dict) -> str:
    location = location_from_cbmc(result.get("sourceLocation"))
    return f"{location.file}:{location.line}" if location and location.file and location.line is not None else "?"


def _probe_display(probe: ProbeProperty, duplicates: dict[str, int]) -> str:
    return f"{probe.label} ({_probe_location(probe.result)})" if probe.label in duplicates else probe.label


def _property_details(probes: list[ProbeProperty]) -> dict[str, dict]:
    return {
        probe.property_id: {
            "label": probe.label,
            "reachable": probe.status == PROBE_REACHED,
            "status": probe.status,
            "location": _probe_location(probe.result),
        }
        for probe in probes
    }


def _reachability_by_label(probes: list[ProbeProperty], source_counts: dict[str, int]) -> dict[str, bool]:
    reachable: dict[str, bool] = {}
    for probe in probes:
        reachable[probe.label] = reachable.get(probe.label, True) and probe.status == PROBE_REACHED
    for label in source_counts:
        reachable.setdefault(label, False)
    return reachable


def _duplicate_labels_note(duplicates: dict[str, int]) -> str:
    return (
        "duplicate LEANC_REACH label(s): "
        + ", ".join(f"{label!r} x{count}" for label, count in sorted(duplicates.items()))
        + "; every probe occurrence is checked separately"
    )


def _undecided_problem(run: CbmcRun, undecided: list[str]) -> ToolProblem:
    excerpt = f": {messages_excerpt(run.output, run.proc.stderr)}" if run.output.errors else ""
    return ToolProblem(
        Outcome.ERROR,
        f"CBMC (vacuity run) could not decide the reachability of probe(s) {', '.join(undecided)}{excerpt}",
    )


def _append_with_unique_id(failures: list[FailureReport], failure: FailureReport) -> None:
    taken = {existing.id for existing in failures}
    base, suffix = failure.id, 2
    while failure.id in taken:
        failure.id = f"{base}#{suffix}"
        suffix += 1
    failures.append(failure)


def _unattributed_probes_failure(count: int, harness: str, raw_ref: str) -> FailureReport:
    return FailureReport(
        id=f"{VACUITY_ID_PREFIX}unreported-probes",
        kind="vacuity",
        tool=TOOL,
        description=(
            f"{count} LEANC_REACH occurrence(s) of the harness {harness} were not reported by CBMC "
            "(compiled out, or in code not reachable from the harness entry point)"
        ),
        location=Location(file=harness),
        explanation=(
            "Each LEANC_REACH occurrence must appear as a probe in CBMC's vacuity run; missing probes are "
            "treated as unreachable, so the verification cannot be shown to be non-vacuous."
        ),
        raw_ref=raw_ref,
    )


@dataclass
class _ProbeJudge:
    run: CbmcRun
    harness: str
    unwind_insufficient: bool
    probes: list[ProbeProperty]
    source_counts: dict[str, int]
    reported: Counter
    duplicates: dict[str, int]
    failures: list[FailureReport] = field(default_factory=list)

    def report_unreachable(self) -> list[str]:
        unreachable = []
        for probe in self.probes:
            if probe.status != PROBE_UNREACHABLE:
                continue
            failure = vacuity_failure(
                probe.label, result=probe.result, raw_ref=self.run.raw_ref, unwind_insufficient=self.unwind_insufficient
            )
            if probe.label in self.duplicates:
                failure.id = f"{VACUITY_ID_PREFIX}{probe.label}@{_probe_location(probe.result)}"
            _append_with_unique_id(self.failures, failure)
            unreachable.append(_probe_display(probe, self.duplicates))
        return unreachable

    def missing_by_label(self) -> dict[str, int]:
        return {
            label: count - self.reported.get(label, 0)
            for label, count in self.source_counts.items()
            if count > self.reported.get(label, 0)
        }

    def report_missing(self, missing_by_label: dict[str, int]) -> None:
        for label, missing in missing_by_label.items():
            failure = vacuity_failure(
                label, raw_ref=self.run.raw_ref, reported=False, unwind_insufficient=self.unwind_insufficient
            )
            if self.reported.get(label, 0):
                failure.id = f"{VACUITY_ID_PREFIX}{label}@unreported"
                failure.description = (
                    f'{missing} of {self.source_counts[label]} occurrence(s) of LEANC_REACH("{label}") were not '
                    "reported by CBMC (the code containing them is not reachable from the harness entry point)"
                )
            _append_with_unique_id(self.failures, failure)

    def report_unattributed(self, unattributed: int) -> None:
        if unattributed > 0:
            _append_with_unique_id(
                self.failures, _unattributed_probes_failure(unattributed, self.harness, self.run.raw_ref)
            )


def _vacuous_reason(unreachable: list[str], missing_count: int, missing_by_label: dict[str, int]) -> str:
    parts = []
    if unreachable:
        parts.append(f"probe(s) {', '.join(unreachable)} unreachable")
    if missing_count:
        names = ", ".join(missing_by_label) if missing_by_label else "with non-literal labels"
        parts.append(f"{missing_count} LEANC_REACH occurrence(s) not reported by CBMC ({names})")
    return (
        "vacuous harness: "
        + "; ".join(parts)
        + " - the assumptions/code make that point unreachable, so the checks there are vacuous"
    )


def evaluate_probes(run: CbmcRun, harness_text: str, *, harness: str, unwind_insufficient: bool) -> ProbeVerdict:
    occurrences, source_counts = probe_occurrences(harness_text)
    probes = _reported_probes(run)
    reported = Counter(probe.label for probe in probes)
    duplicates = {label: count for label, count in reported.items() if count > 1}
    details: dict[str, Any] = {
        "status": "ok",
        "probes": _reachability_by_label(probes, source_counts),
        "properties": _property_details(probes),
        "occurrences": occurrences,
    }
    verdict = ProbeVerdict(details=details)
    if duplicates:
        details["duplicate_labels"] = duplicates
        verdict.notes.append(_duplicate_labels_note(duplicates))

    undecided = [_probe_display(probe, duplicates) for probe in probes if probe.status not in PROBE_DECIDED]
    if undecided:
        details["status"] = "skipped"
        details["note"] = "undecided probes"
        verdict.problem = _undecided_problem(run, undecided)
        return verdict

    judge = _ProbeJudge(run, harness, unwind_insufficient, probes, source_counts, reported, duplicates)
    unreachable = judge.report_unreachable()
    missing_by_label = judge.missing_by_label()
    judge.report_missing(missing_by_label)
    missing_total = max(0, occurrences - len(probes))
    judge.report_unattributed(missing_total - sum(missing_by_label.values()))
    verdict.failures = judge.failures
    details["unreported_occurrences"] = max(missing_total, sum(missing_by_label.values()))

    if not probes and not verdict.failures:
        details["status"] = "unchecked"
        verdict.inconclusive = (
            "the harness mentions LEANC_REACH but CBMC reported no probe in the vacuity run; vacuity cannot be checked"
        )
        return verdict
    if verdict.failures:
        details["status"] = "vacuous"
        verdict.inconclusive = _vacuous_reason(unreachable, details["unreported_occurrences"], missing_by_label)
    return verdict
