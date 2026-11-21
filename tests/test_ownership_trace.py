from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

from leanc.backends.sanitizer import ownership
from leanc.backends.sanitizer.ownership_analysis import (
    LOST_DURING_CALL,
    MAX_TRACED_ALLOCATIONS,
    NOT_LOCALISED,
    UNREACHABLE_BY_NEXT_EVENT,
    UNREACHABLE_BY_RETURN,
    build_trace,
    call_chain,
    leaked_allocations,
    release_summary,
)
from leanc.backends.sanitizer.ownership_attach import TraceCandidate, attach_traces
from leanc.backends.sanitizer.ownership_log import (
    ProbeKey,
    leaked_objects_at_exit,
    leaked_objects_per_probe,
    parse_events,
)
from leanc.backends.sanitizer.ownership_replay import (
    MAX_LOG_BYTES,
    read_trace_log,
    replay_env,
)
from leanc.backends.sanitizer.ownership_search import (
    MAX_PROBED_EXITS,
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
    parse_symbolizer_output,
)
from leanc.foundation.models import (
    OWNERSHIP_LOSS_TEXT,
    OWNERSHIP_TRACE_LABEL,
    CounterexampleInput,
    FailureReport,
    Location,
    MemoryOwnershipTrace,
    OwnershipLoss,
)
from leanc.llm.prompts.rendering import render_failure
from leanc.reports.json_report import _failure_entry

P1, P2 = "0x502000000010", "0x502000000030"
MAKE, CALLER, SETTER = "0x100", "0x200", "0x300"
MAKE_SITE, CALLER_SITE, SETTER_SITE = "0x1075ca", "0x10800", "0x10900"
ALLOC_SITE, FREE_SITE, REALLOC_SITE = "0x10683c", "0x10733a", "0x10777a"


def _report(*pointers: str, kind: str = "Direct") -> str:
    objects = "".join(f"{p} (6 bytes)\n" for p in pointers)
    return (
        f"{kind} leak of 6 byte(s) in {len(pointers)} object(s) allocated from:\n    #0 0x1 in malloc\n"
        f"Objects leaked above:\n{objects}SUMMARY: AddressSanitizer: 6 byte(s) leaked in 1 allocation(s)."
    )


def _events(text: str):
    events = parse_events(text)
    assert not isinstance(events, str), events
    return events


def _trace(text: str):
    run = build_trace(_events(text))
    assert not isinstance(run, str), run
    return run


def _probe_stderr(text: str, listed: dict[tuple[int, str], tuple[str, ...]]) -> str:
    out = []
    for line in text.splitlines():
        if line.startswith("C "):
            _, ordinal, phase, result = line.split()
            out.append(f"LEANC-OWNERSHIP-CHECK {ordinal} {phase}")
            if result == "1":
                out.append(_report(*listed[(int(ordinal), phase)]))
    return "\n".join(out) + "\n"


def _classify(record_text: str, probe_text: str, listed, direct=(P1,)):
    record = _trace(record_text)
    selection = leaked_allocations(record, frozenset(int(p, 16) for p in direct))
    events = _events(probe_text)
    probes = leaked_objects_per_probe(_probe_stderr(probe_text, listed), events)
    assert not isinstance(probes, str), probes
    problem = replay_matches(record, build_trace(events), selection.allocations)
    if problem:
        return problem
    searches = start_searches(record, selection.allocations)
    probed = {o for o, _ in record.exit_positions.items()}
    for search in searches:
        narrow(search, probed, probes)
    return [classify(record, search) for search in searches]


def _without_checks(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.startswith("C ")) + "\n"


CALL = f"E {MAKE} {MAKE_SITE}\nA {P1} 6 {ALLOC_SITE}\nX 1 {MAKE} {MAKE_SITE}\n"


def test_parse_events_rejects_anything_that_is_not_a_trace_event():
    assert isinstance(parse_events(f"A {P1} 6 {ALLOC_SITE}\nrm -rf /\n"), str)
    assert isinstance(parse_events(f"A {P1} -6 {ALLOC_SITE}\n"), str)
    assert isinstance(parse_events(f"A 0x{'f' * 17} 6 {ALLOC_SITE}\n"), str)


def test_listing_at_the_exit_of_a_frame_on_the_allocation_stack_is_lost_during_call():
    probe = CALL + "C 1 in 1\n"
    [finding] = _classify(CALL, probe, {(1, "in"): (P1,)})
    assert finding.loss == LOST_DURING_CALL and finding.frame.function == int(MAKE, 16)


def test_listing_only_at_the_next_event_is_not_blamed_on_the_callee_frame():
    probe = CALL + f"C 1 in 0\nC 1 after 1\nA {P2} 4 {ALLOC_SITE}\n"
    [finding] = _classify(_without_checks(probe), probe, {(1, "after"): (P1,)})
    assert finding.loss == UNREACHABLE_BY_NEXT_EVENT and finding.frame.function == int(MAKE, 16)


def test_reachable_scans_are_never_evidence_that_the_caller_held_the_block():
    probe = CALL + f"C 1 in 0\nC 1 after 0\nA {P2} 4 {ALLOC_SITE}\n"
    [finding] = _classify(_without_checks(probe), probe, {})
    assert finding.loss == NOT_LOCALISED and finding.frame is None


def test_a_later_function_that_drops_the_only_reference_is_found():
    probe = CALL + f"C 1 in 0\nC 1 after 0\nE {SETTER} {SETTER_SITE}\nX 2 {SETTER} {SETTER_SITE}\nC 2 in 1\n"
    record = _without_checks(probe)
    run = _trace(record)
    assert next_probes(start_searches(run, leaked_allocations(run, frozenset({int(P1, 16)})).allocations)) == [1, 2]
    [finding] = _classify(record, probe, {(2, "in"): (P1,)})
    assert finding.loss == UNREACHABLE_BY_RETURN and finding.frame.function == int(SETTER, 16)


def test_diverged_replay_and_unbalanced_events_yield_no_finding():
    assert isinstance(_classify(CALL, CALL.replace(" 6 ", " 7 ") + "C 1 in 1\n", {(1, "in"): (P1,)}), str)
    assert isinstance(build_trace(_events(f"E {MAKE} {MAKE_SITE}\nX 1 {CALLER} {MAKE_SITE}\n")), str)


def test_inlined_callee_is_flagged_so_no_return_site_is_claimed():
    nested = (
        f"E {CALLER} {CALLER_SITE}\nE {MAKE} {CALLER_SITE}\nA {P1} 6 {ALLOC_SITE}\n"
        f"X 1 {MAKE} {CALLER_SITE}\nX 2 {CALLER} {CALLER_SITE}\n"
    )
    probe = nested.replace(f"X 1 {MAKE} {CALLER_SITE}\n", f"X 1 {MAKE} {CALLER_SITE}\nC 1 in 1\n")
    [finding] = _classify(nested, probe, {(1, "in"): (P1,)})
    assert finding.inlined


def test_indirect_only_leaks_are_not_traced():
    run = _trace(CALL + f"A {P2} 6 {ALLOC_SITE}\n")
    selection = leaked_allocations(run, frozenset(), frozenset({int(P1, 16)}))
    assert selection.allocations == [] and selection.indirect_only == 1


def test_block_allocated_by_the_driver_and_dropped_by_editable_code_is_traced():
    probe = f"A {P1} 6 {ALLOC_SITE}\nE {SETTER} {SETTER_SITE}\nX 1 {SETTER} {SETTER_SITE}\nC 1 in 1\n"
    record = _without_checks(probe)
    [allocation] = leaked_allocations(_trace(record), frozenset({int(P1, 16)})).allocations
    assert allocation.frames == ()
    [finding] = _classify(record, probe, {(1, "in"): (P1,)})
    assert finding.loss == UNREACHABLE_BY_RETURN and finding.frame.function == int(SETTER, 16) and finding.chain == ()


def _recursion(depth: int) -> str:
    enters = "".join(f"E {MAKE} {MAKE_SITE}\n" for _ in range(depth))
    exits = "".join(f"X {i} {MAKE} {MAKE_SITE}\n" for i in range(1, depth + 1))
    return enters + f"A {P1} 6 {ALLOC_SITE}\n" + exits


def test_multi_round_search_finds_a_loss_deep_in_a_recursion():
    depth, lost_at = 500, 437
    record = _trace(_recursion(depth))
    [allocation] = leaked_allocations(record, frozenset({int(P1, 16)})).allocations
    [search] = start_searches(record, [allocation])
    for _ in range(MAX_SEARCH_ROUNDS):
        probed = next_probes([search])
        assert len(probed) <= MAX_PROBED_EXITS
        listed = {ProbeKey(o, "in"): frozenset({int(P1, 16)}) for o in probed if o >= lost_at}
        narrow(search, set(probed), listed)
    assert search.found is not None and search.found.exit_ordinal >= lost_at
    finding = classify(record, search)
    assert finding.loss == LOST_DURING_CALL
    assert search.found.exit_ordinal - lost_at < 2


def test_call_sites_outside_the_image_truncate_the_chain():
    run = _trace(f"E {CALLER} 0x0\nE {MAKE} {MAKE_SITE}\nA {P1} 6 {ALLOC_SITE}\nX 1 {MAKE} {MAKE_SITE}\nX 2 {CALLER} 0x0\n")
    [allocation] = leaked_allocations(run, frozenset({int(P1, 16)})).allocations
    assert call_chain(run, allocation) == (int(MAKE_SITE, 16),)


def test_events_from_a_second_thread_stop_the_trace_with_a_clear_reason():
    problem = build_trace(_events(CALL + "T\n"))
    assert isinstance(problem, str) and "several threads" in problem


def test_distinct_leaks_are_capped_and_counted():
    count = MAX_TRACED_ALLOCATIONS + 3
    text = "".join(
        f"E {MAKE} 0x{0x1000 + i:x}\nA 0x{0x5000 + 16 * i:x} 6 {ALLOC_SITE}\nX {i + 1} {MAKE} 0x{0x1000 + i:x}\n"
        for i in range(count)
    )
    selection = leaked_allocations(_trace(text), frozenset(0x5000 + 16 * i for i in range(count)))
    assert len(selection.allocations) == MAX_TRACED_ALLOCATIONS and selection.dropped == 3


def test_failed_realloc_keeps_the_block_live_and_is_not_a_release():
    text = f"E {MAKE} {MAKE_SITE}\nA {P1} 16 {ALLOC_SITE}\nR {P1} 0x0 4611686018427387903 {REALLOC_SITE}\nX 1 {MAKE} {MAKE_SITE}\n"
    run = _trace(text)
    assert run.live == {int(P1, 16): 0} and release_summary([run]) == {}


def test_successful_realloc_moves_the_block_and_only_free_counts_as_release():
    moved = _trace(f"A {P1} 16 {ALLOC_SITE}\nR {P1} {P2} 32 {REALLOC_SITE}\nF {P2} {FREE_SITE}\n")
    assert release_summary([moved]) == {int(REALLOC_SITE, 16): [int(FREE_SITE, 16)]}
    shrunk = _trace(f"A {P1} 16 {ALLOC_SITE}\nR {P1} 0x0 0 {REALLOC_SITE}\n")
    assert release_summary([shrunk]) == {int(ALLOC_SITE, 16): [int(REALLOC_SITE, 16)]}


def test_probe_result_zero_ignores_reports_printed_on_stderr():
    events = _events("E 0x1 0x2\nX 1 0x1 0x2\nC 1 in 0\n")
    probes = leaked_objects_per_probe(f"LEANC-OWNERSHIP-CHECK 1 in\n{_report(P1)}\n", events)
    assert probes == {ProbeKey(1, "in"): frozenset()}


def test_only_direct_objects_of_the_first_report_after_a_marker_count():
    events = _events("E 0x1 0x2\nX 1 0x1 0x2\nC 1 in 1\n")
    stderr = f"LEANC-OWNERSHIP-CHECK 1 in\n{_report(P1)}\n{_report(P2, kind='Indirect')}\n{_report(P2)}\n"
    assert leaked_objects_per_probe(stderr, events) == {ProbeKey(1, "in"): frozenset({int(P1, 16)})}


@pytest.mark.parametrize(
    "stderr",
    [
        "LEANC-OWNERSHIP-CHECK 2 in\n",
        "",
        "LEANC-OWNERSHIP-CHECK 1 in\nLEANC-OWNERSHIP-CHECK 1 in\n",
        "LEANC-OWNERSHIP-CHECK 1 in\n0x1 (1 bytes)\n",
    ],
)
def test_forged_or_inconsistent_probe_output_is_rejected(stderr):
    assert isinstance(leaked_objects_per_probe(stderr, _events("E 0x1 0x2\nX 1 0x1 0x2\nC 1 in 1\n")), str)


def test_recording_run_separates_direct_and_indirect_objects():
    leaked = leaked_objects_at_exit(f"{_report(P1)}\n{_report(P2, kind='Indirect')}\n")
    assert leaked.direct == frozenset({int(P1, 16)}) and leaked.indirect == frozenset({int(P2, 16)})
    assert isinstance(leaked_objects_at_exit("LEANC-OWNERSHIP-CHECK 1 in\n"), str)


SYMBOLIZER_JSON = json.dumps([
    {"Address": "0x10683b", "Symbol": [{"FileName": "/s/snapshot/src/join.c", "FunctionName": "join_with_sep", "Line": 22,
                                        "Column": 17, "StartFileName": "/s/snapshot/src/join.c", "StartLine": 5}]},
    {"Address": "0x1", "Symbol": [{"FileName": "/usr/include/x.h", "FunctionName": "x", "Line": 3, "Column": 0}]},
])


def test_symbolizer_output_maps_to_project_locations_only():
    addresses = [CodeAddress(0x10683C, True), CodeAddress(0x1, False)]
    result = parse_symbolizer_output(SYMBOLIZER_JSON, addresses, {"src/join.c"}, "/s/snapshot")
    assert result.locations[addresses[0]] == Location(file="src/join.c", line=22, column=17, function="join_with_sep")
    assert result.functions[addresses[0]] == Location(file="src/join.c", line=5, function="join_with_sep")
    assert addresses[1] not in result.locations


@pytest.mark.parametrize("text", ["not json", "[]", json.dumps([{"Symbol": "x"}])])
def test_malformed_symbolizer_output_is_a_problem_or_ignored(text):
    result = parse_symbolizer_output(text, [CodeAddress(0x10, True)], {"src/join.c"}, "/s")
    assert result.problem is not None or not result.locations


def test_return_addresses_are_symbolized_one_byte_earlier():
    assert CodeAddress(0x10683C, True).lookup == 0x10683B and CodeAddress(0x106758, False).lookup == 0x106758


def test_every_loss_kind_has_a_description():
    assert set(OWNERSHIP_LOSS_TEXT) == set(OwnershipLoss.__args__)


SITE = Location(file="src/join.c", line=22, function="join_with_sep")
PROJECT_FILES = {"src/join.c", "tests/test_join.c"}


def _trace_model(loss=LOST_DURING_CALL, function="join_with_sep") -> MemoryOwnershipTrace:
    return MemoryOwnershipTrace(
        test_case="separator_in_field",
        allocation=SITE,
        released_at=[Location(file="tests/test_join.c", line=28, function="expect_ok")],
        loss=loss,
        function=Location(file="src/join.c", line=5, function=function),
        returned_to=Location(file="tests/test_join.c", line=38, function="expect_err"),
    )


def _failure(trace=None, callers=("tests/test_join.c:38:14",), suffix="") -> FailureReport:
    return FailureReport(
        id="sanitizer:separator_in_field:lsan:memory-leak" + suffix,
        kind="runtime",
        tool="lsan",
        property_id="memory-leak",
        description="LeakSanitizer: Direct leak of 6 byte(s)",
        location=SITE,
        inputs=[CounterexampleInput(name="test_case", value="separator_in_field")],
        call_stack=[
            "#0 malloc /src/compiler-rt/asan_malloc_linux.cpp:69:3",
            "#1 join_with_sep src/join.c:22:17",
            *(f"#{i + 2} caller {c}" for i, c in enumerate(callers)),
        ],
        ownership_trace=trace,
    )


def test_traces_attach_by_allocation_stack_not_by_line_alone():
    first = TraceCandidate(_trace_model(function="a"), (("src/join.c", 22), ("tests/test_join.c", 38)))
    second = TraceCandidate(_trace_model(function="b"), (("src/join.c", 22), ("tests/test_join.c", 52)))
    failures = [_failure(), _failure(callers=("tests/test_join.c:52:3",), suffix="#2")]
    attach_traces([first, second], failures, PROJECT_FILES)
    assert [f.ownership_trace.function.function for f in failures] == ["a", "b"]


def test_conflicting_matches_attach_nothing():
    first = TraceCandidate(_trace_model(function="a"), (("src/join.c", 22),))
    second = TraceCandidate(_trace_model(function="b"), (("src/join.c", 22),))
    failure = _failure()
    notes = attach_traces([first, second], [failure], PROJECT_FILES)
    assert failure.ownership_trace is None and "conflicting" in notes[0]


def test_released_at_is_deduplicated_after_symbolization():
    session = SimpleNamespace(releases={1: [10, 11]})
    finding = SimpleNamespace(allocation=SimpleNamespace(site=1))
    same = Location(file="tests/test_join.c", line=28, column=9, function="expect_ok")
    symbols = Symbolized(locations={CodeAddress(10, True): same, CodeAddress(11, True): same})
    assert ownership._released_at(session, finding, symbols) == [same]


def test_budget_exhaustion_leaves_a_reason_for_every_target_case():
    cases = [SimpleNamespace(name=n) for n in ("normal", "separator_in_field", "nul_in_field")]
    spec = SimpleNamespace(tests=SimpleNamespace(cases=cases))
    replays = SimpleNamespace(inputs=SimpleNamespace(ctx=SimpleNamespace(spec=spec)))
    session = ownership._Session(replays, "symbolizer", time.monotonic() - 1)
    per_case: dict = {}
    note = ownership._trace_cases(session, {"separator_in_field": [], "nul_in_field": []}, per_case)
    assert "budget" in note and set(per_case) == {"separator_in_field", "nul_in_field"}


def test_prompt_shows_the_labelled_trace_without_addresses():
    text = render_failure(_failure(_trace_model()))
    assert OWNERSHIP_TRACE_LABEL in text and "released at tests/test_join.c:28" in text and "0x" not in text
    assert render_failure(_failure(_trace_model())) == text
    assert OWNERSHIP_TRACE_LABEL not in render_failure(_failure())


def test_json_summary_entry_carries_the_trace():
    assert _failure_entry("sanitizer", _failure(_trace_model()))["ownership_trace"]["loss"] == LOST_DURING_CALL
    assert _failure_entry("sanitizer", _failure())["ownership_trace"] is None


def test_leak_failures_are_grouped_by_test_case():
    other = _failure().model_copy(update={"tool": "asan", "property_id": "heap-buffer-overflow"})
    assert list(ownership.leak_failures_by_case([_failure(), other])) == ["separator_in_field"]


def test_trace_log_reader_refuses_symlinks_fifos_and_oversized_files(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("A 0x1 1 0x1\n")
    link = tmp_path / "link.log"
    link.symlink_to(secret)
    fifo = tmp_path / "fifo.log"
    os.mkfifo(fifo)
    big = tmp_path / "big.log"
    big.write_bytes(b"A" * (MAX_LOG_BYTES + 1))
    for path in (link, fifo, big, tmp_path / "missing.log"):
        assert read_trace_log(path).problem is not None
    assert read_trace_log(secret).text == "A 0x1 1 0x1\n"


def test_compact_replay_reports_keep_two_frames_because_lsan_drops_single_frame_leaks(tmp_path):
    inputs = SimpleNamespace(env={"ASAN_OPTIONS": "detect_leaks=1", "LSAN_OPTIONS": "exitcode=88"})
    env = replay_env(inputs, tmp_path / "ownership.log", [3, 5])
    assert "malloc_context_size=2" in env["ASAN_OPTIONS"] and "malloc_context_size=2" in env["LSAN_OPTIONS"]
    assert "report_objects=1" in env["LSAN_OPTIONS"] and env["LEANC_OWNERSHIP_CHECKS"] == "3,5"
    assert inputs.env == {"ASAN_OPTIONS": "detect_leaks=1", "LSAN_OPTIONS": "exitcode=88"}
