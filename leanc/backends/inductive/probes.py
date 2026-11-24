from __future__ import annotations

import posixpath
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from leanc.backends.cbmc.execution import CbmcRun
from leanc.backends.cbmc.vacuity import PROBE_DECIDED, PROBE_REACHED, evaluate_probes
from leanc.backends.counterexample.locations import location_from_cbmc
from leanc.backends.counterexample.properties import CBMC_TOOL, VACUITY_ID_PREFIX, is_reach_probe
from leanc.backends.inductive.findings import FAILURE_ID_PREFIX, UNPROVED_KIND, Finding
from leanc.backends.inductive.harness_calls import RETURN_PROBE_PREFIX, CallSite, return_probe_label
from leanc.backends.inductive.problems import BuildProblem, tool_error
from leanc.foundation.models import FailureReport, Location
from leanc.foundation.reasons import Reason
from leanc.rules.loop_sites import (
    FUNCTION_PROBE_PREFIX,
    LOOP_PROBE_PREFIX,
    FunctionEntry,
    MatchedLoop,
    function_probe_label,
    loop_probe_label,
)

INSERTED_PROBE_PREFIXES = (FUNCTION_PROBE_PREFIX, LOOP_PROBE_PREFIX, RETURN_PROBE_PREFIX)
VACUITY_FAILURE_PREFIX = f"{FAILURE_ID_PREFIX}vacuity:"
UNCHECKED_STATUS = "unchecked"
VACUOUS_STATUS = "vacuous"
OK_STATUS = "ok"
NOT_REPORTED = "not reported by CBMC (dropped as unreachable from main)"
UNREACHABLE = "unreachable under the harness assumptions"
NO_PROBE_REPORTED = "CBMC reported no probe in the vacuity run of the inductive harness; vacuity cannot be checked"
VACUOUS_EXPLANATION = (
    "The vacuity run of the inductive gate turns every probe into an assertion that must fail (reached). A probe "
    "that holds or is missing means that point is unreachable from the harness assumptions, so every check "
    "after it holds vacuously."
)


@dataclass(frozen=True)
class ExpectedProbe:
    label: str
    count: int
    files: frozenset[str]


@dataclass
class ProbeJudgement:
    details: dict[str, Any] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    problem: BuildProblem | None = None


def _return_probes(calls: Sequence[CallSite]) -> list[ExpectedProbe]:
    counts = Counter(call.function for call in calls)
    files: dict[str, set[str]] = {}
    for call in calls:
        files.setdefault(call.function, set()).add(call.origin.file if call.origin else "")
    return [
        ExpectedProbe(return_probe_label(name), count, frozenset(files[name]) if "" not in files[name] else frozenset())
        for name, count in counts.items()
    ]


def expected_probes(
    entries: Sequence[FunctionEntry], contracts: Sequence[MatchedLoop], calls: Sequence[CallSite]
) -> list[ExpectedProbe]:
    expected = [ExpectedProbe(function_probe_label(entry.name), 1, frozenset({entry.file})) for entry in entries]
    expected += [
        ExpectedProbe(loop_probe_label(item.site.file, item.site.line), 1, frozenset({item.site.file})) for item in contracts
    ]
    return expected + _return_probes(calls)


def _description(result: dict) -> str:
    return str(result.get("description") or "")


def _inserted(results: Sequence[dict]) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for result in results:
        description = _description(result)
        if description.startswith(INSERTED_PROBE_PREFIXES):
            found.setdefault(description, []).append(result)
    return found


def _in_files(result: dict, files: frozenset[str]) -> bool:
    if not files:
        return True
    location = location_from_cbmc(result.get("sourceLocation"))
    allowed = {posixpath.normpath(file) for file in files}
    return location is not None and location.file is not None and posixpath.normpath(location.file) in allowed


def _first_file(probe: ExpectedProbe) -> Location | None:
    return Location(file=min(probe.files)) if probe.files else None


def _status(result: dict) -> str:
    return str(result.get("status", "")).upper()


def _vacuous_failure(label: str, description: str, location: Location | None, raw_ref: str) -> FailureReport:
    return FailureReport(
        id=f"{VACUITY_FAILURE_PREFIX}{label}",
        kind=UNPROVED_KIND,
        tool=CBMC_TOOL,
        property_id=label,
        description=description,
        location=location,
        explanation=VACUOUS_EXPLANATION,
        raw_ref=raw_ref,
    )


def _vacuous(label: str, description: str, location: Location | None, raw_ref: str) -> Finding:
    failure = _vacuous_failure(label, description, location, raw_ref)
    return Finding(Reason.VACUOUS_HARNESS, f"{label}: {description}", location, failure)


def _judge_inserted(judgement: ProbeJudgement, run: CbmcRun, expected: Sequence[ExpectedProbe]) -> dict[str, dict]:
    reported = _inserted(run.output.results or [])
    unexpected = sorted(set(reported) - {probe.label for probe in expected})
    if unexpected:
        judgement.problem = tool_error("the vacuity run reports probes that Lean C did not insert", unexpected)
    summary: dict[str, dict] = {}
    for probe in expected:
        results = reported.get(probe.label, [])
        undecided = [result for result in results if _status(result) not in PROBE_DECIDED]
        if undecided and judgement.problem is None:
            judgement.problem = tool_error(f"CBMC could not decide the reachability of probe {probe.label}")
        reached = sum(_status(result) == PROBE_REACHED for result in results)
        misplaced = [result for result in results if not _in_files(result, probe.files)]
        if misplaced and judgement.problem is None:
            judgement.problem = tool_error(f"probe {probe.label} is reported outside {', '.join(sorted(probe.files))}")
        summary[probe.label] = {"expected": probe.count, "reported": len(results), "reached": reached}
        location = location_from_cbmc(results[0].get("sourceLocation")) if results else _first_file(probe)
        if len(results) < probe.count:
            missing = f"{probe.count - len(results)} of {probe.count} occurrence(s) {NOT_REPORTED}"
            judgement.findings.append(_vacuous(probe.label, missing, location, run.raw_ref))
        elif reached < len(results):
            unreached = f"{len(results) - reached} of {len(results)} occurrence(s) {UNREACHABLE}"
            judgement.findings.append(_vacuous(probe.label, unreached, location, run.raw_ref))
    return summary


def _rekeyed(failure: FailureReport) -> Finding:
    label = failure.id.removeprefix(VACUITY_ID_PREFIX)
    vacuous = _vacuous_failure(label, failure.description, failure.location, failure.raw_ref or "")
    vacuous.explanation = failure.explanation
    return Finding(Reason.VACUOUS_HARNESS, f"{label}: {failure.description}", failure.location, vacuous)


def judge_probes(
    run: CbmcRun, harness_text: str, harness: str, expected: Sequence[ExpectedProbe], unwind_insufficient: bool
) -> ProbeJudgement:
    judgement = ProbeJudgement()
    verdict = evaluate_probes(run, harness_text, harness=harness, unwind_insufficient=unwind_insufficient)
    judgement.details = dict(verdict.details)
    judgement.notes = list(verdict.notes)
    if verdict.problem is not None:
        judgement.problem = tool_error(verdict.problem.summary)
        return judgement
    judgement.details["inserted"] = _judge_inserted(judgement, run, expected)
    if judgement.problem is not None:
        return judgement
    results = run.output.results or []
    if not any(is_reach_probe(result) or _description(result).startswith(INSERTED_PROBE_PREFIXES) for result in results):
        judgement.details["status"] = UNCHECKED_STATUS
        judgement.findings = [Finding(Reason.VACUITY_UNCHECKED, NO_PROBE_REPORTED)]
        return judgement
    judgement.findings = [_rekeyed(failure) for failure in verdict.failures] + judgement.findings
    if verdict.details.get("status") == UNCHECKED_STATUS and not judgement.findings:
        judgement.details["status"] = UNCHECKED_STATUS
        judgement.findings = [Finding(Reason.VACUITY_UNCHECKED, verdict.inconclusive)]
        return judgement
    judgement.details["status"] = VACUOUS_STATUS if judgement.findings else OK_STATUS
    return judgement
