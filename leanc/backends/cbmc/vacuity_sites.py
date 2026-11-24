from __future__ import annotations

import posixpath
from collections import Counter
from collections.abc import Sequence

from leanc.backends.cbmc.vacuity_divergence import ReachSite
from leanc.backends.counterexample.properties import REACH_PREFIX, VACUITY_ID_PREFIX, is_reach_probe
from leanc.backends.counterexample.reports import vacuity_failure
from leanc.foundation.models import FailureReport, Location

__all__ = ["SITE_ID_INFIX", "UNREPORTED_SITE", "site_failures", "unreported_reason", "unreported_sites"]

SiteKey = tuple[str, int, str]
SITE_ID_INFIX = "@site:"
MAX_LISTED_SITES = 3
UNREPORTED_SITE = (
    "is a LEANC_REACH site of the preprocessed vacuity program that CBMC did not report as a probe at its line "
    "with its label (dropped as unreachable code, or the call does not reach the CBMC built-in)"
)


def _site_key(site: ReachSite) -> SiteKey | None:
    return None if site.origin is None else (posixpath.normpath(site.origin.file), site.origin.line, site.label)


def _result_key(result: dict) -> SiteKey | None:
    location = result.get("sourceLocation")
    if not isinstance(location, dict):
        return None
    file, line = location.get("file"), str(location.get("line", ""))
    if not isinstance(file, str) or not file or not line.isdigit():
        return None
    return posixpath.normpath(file), int(line), str(result.get("description"))[len(REACH_PREFIX) :]


def unreported_sites(results: Sequence[dict], sites: Sequence[ReachSite], flagged_labels: Sequence[str]) -> list[ReachSite]:
    reported = Counter(key for result in results if is_reach_probe(result) and (key := _result_key(result)) is not None)
    unreported = []
    for site in sites:
        if site.label in flagged_labels:
            continue
        key = _site_key(site)
        if key is None or reported[key] == 0:
            unreported.append(site)
            continue
        reported[key] -= 1
    return unreported


def _site_failure(site: ReachSite, raw_ref: str, unwind_insufficient: bool) -> FailureReport:
    failure = vacuity_failure(site.label, raw_ref=raw_ref, reported=False, unwind_insufficient=unwind_insufficient)
    failure.id = f"{VACUITY_ID_PREFIX}{site.label}{SITE_ID_INFIX}{site.where}"
    failure.description = f"{site.describe()} {UNREPORTED_SITE}"
    failure.location = Location(file=site.origin.file, line=site.origin.line) if site.origin else None
    return failure


def site_failures(sites: Sequence[ReachSite], raw_ref: str, unwind_insufficient: bool) -> list[FailureReport]:
    failures: list[FailureReport] = []
    seen: Counter[str] = Counter()
    for site in sites:
        failure = _site_failure(site, raw_ref, unwind_insufficient)
        seen[failure.id] += 1
        if seen[failure.id] > 1:
            failure.id = f"{failure.id}#{seen[failure.id]}"
        failures.append(failure)
    return failures


def unreported_reason(sites: Sequence[ReachSite]) -> str:
    shown = ", ".join(site.describe() for site in sites[:MAX_LISTED_SITES])
    more = f" (+{len(sites) - MAX_LISTED_SITES} more)" if len(sites) > MAX_LISTED_SITES else ""
    return f"vacuous harness: {len(sites)} LEANC_REACH site(s) of the vacuity program not reported by CBMC: {shown}{more}"
