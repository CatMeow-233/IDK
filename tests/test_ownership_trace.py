from __future__ import annotations

import json
import os

import pytest

from leanc.backends.sanitizer.ownership import _read_trace_log, leak_failures_by_case
from leanc.backends.sanitizer.ownership_analysis import (
    LOSS_AFTER_RETURN_BY_CALLER,
    LOSS_AT_FUNCTION_RETURN,
    LOSS_AT_OR_AFTER_RETURN,
    LOSS_INSIDE_FUNCTION,
    build_trace,
    candidate_exits,
    classify_losses,
    leaked_allocations,
    probe_plan,
    release_sites,
)
from leanc.backends.sanitizer.ownership_log import (
    ProbeKey,
    leaked_objects_at_exit,
    leaked_objects_per_probe,
    parse_events,
)
from leanc.backends.sanitizer.ownership_symbols import (
    CodeAddress,
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

POINTER = "0x502000000010"
INNER, OUTER, CALL_SITE, OUTER_SITE, ALLOC_SITE, FREE_SITE = "0x100", "0x200", "0x1075ca", "0x10800", "0x10683c", "0x10733a"
REPORT = f"Direct leak of 6 byte(s) in 1 object(s) allocated from:\n    #0 0x1 in malloc\n{POINTER} (6 bytes)\nSUMMARY: AddressSanitizer: 6 byte(s) leaked in 1 allocation(s)."


def _events(text: str):
    events = parse_events(text)
    assert not isinstance(events, str), events
    return events


def _trace(text: str):
    run = build_trace(_events(text))
    assert not isinstance(run, str), run
    return run


ONE_FRAME = f"E {INNER} {CALL_SITE}\nA {POINTER} 6 {ALLOC_SITE}\nX 1 {INNER} {CALL_SITE}\n"
TWO_FRAMES = f"E {OUTER} {OUTER_SITE}\nE {INNER} {CALL_SITE}\nA {POINTER} 6 {ALLOC_SITE}\nX 1 {INNER} {CALL_SITE}\nX 2 {OUTER} {OUTER_SITE}\n"


def _stderr(checks: list[tuple[int, str, int]]) -> str:
    return "".join(f"LEANC-OWNERSHIP-CHECK {o} {phase}\n" + (REPORT + "\n" if r else "") for o, phase, r in checks)


def _classify(record_text: str, probe_text: str, checks):
    record = _trace(record_text)
    leaked = leaked_allocations(record, frozenset({int(POINTER, 16)}))
    probe_events = _events(probe_text)
    probes = leaked_objects_per_probe(_stderr(checks), probe_events)
    assert not isinstance(probes, str), probes
    probe = build_trace(probe_events)
    return classify_losses(record, probe, leaked, probes)


def test_parse_events_rejects_anything_that_is_not_a_trace_event():
    assert isinstance(parse_events(f"A {POINTER} 6 {ALLOC_SITE}\nrm -rf /\n"), str)
    assert isinstance(parse_events(f"A {POINTER} -6 {ALLOC_SITE}\n"), str)
    assert isinstance(parse_events(f"A 0x{'f' * 17} 6 {ALLOC_SITE}\n"), str)
    assert len(_events(ONE_FRAME)) == 3


def test_leak_is_attributed_inside_the_function_when_its_own_exit_probe_sees_it():
    checks = [(1, "in", 1)]
    probe = f"E {INNER} {CALL_SITE}\nA {POINTER} 6 {ALLOC_SITE}\nX 1 {INNER} {CALL_SITE}\nC 1 in 1\n"
    [finding] = _classify(ONE_FRAME, probe, checks)
    assert finding.loss == LOSS_INSIDE_FUNCTION and finding.frame.function == int(INNER, 16)


def test_stale_frame_copy_is_attributed_to_the_function_return_not_the_caller():
    checks = [(1, "in", 0), (1, "after", 1)]
    record = ONE_FRAME + f"A 0x502000000030 4 {ALLOC_SITE}\n"
    probe = f"E {INNER} {CALL_SITE}\nA {POINTER} 6 {ALLOC_SITE}\nX 1 {INNER} {CALL_SITE}\nC 1 in 0\nC 1 after 1\nA 0x502000000030 4 {ALLOC_SITE}\n"
    [finding] = _classify(record, probe, checks)
    assert finding.loss == LOSS_AT_FUNCTION_RETURN


def test_caller_is_blamed_only_when_a_probe_after_the_return_still_saw_the_block():
    checks = [(1, "in", 0), (1, "after", 0), (2, "in", 0), (2, "after", 0)]
    record = TWO_FRAMES + f"A 0x502000000050 4 {ALLOC_SITE}\n"
    probe = (
        f"E {OUTER} {OUTER_SITE}\nE {INNER} {CALL_SITE}\nA {POINTER} 6 {ALLOC_SITE}\nX 1 {INNER} {CALL_SITE}\nC 1 in 0\n"
        f"C 1 after 0\nX 2 {OUTER} {OUTER_SITE}\nC 2 in 0\nC 2 after 0\nA 0x502000000050 4 {ALLOC_SITE}\n"
    )
    [finding] = _classify(record, probe, checks)
    assert finding.loss == LOSS_AFTER_RETURN_BY_CALLER and finding.frame.function == int(OUTER, 16)


def test_without_a_later_event_the_loss_is_reported_as_ambiguous():
    checks = [(1, "in", 0)]
    probe = f"E {INNER} {CALL_SITE}\nA {POINTER} 6 {ALLOC_SITE}\nX 1 {INNER} {CALL_SITE}\nC 1 in 0\n"
    [finding] = _classify(ONE_FRAME, probe, checks)
    assert finding.loss == LOSS_AT_OR_AFTER_RETURN


def test_diverged_replay_yields_no_finding():
    probe = f"E {INNER} {CALL_SITE}\nA {POINTER} 7 {ALLOC_SITE}\nX 1 {INNER} {CALL_SITE}\nC 1 in 1\n"
    assert isinstance(_classify(ONE_FRAME, probe, [(1, "in", 1)]), str)


def test_unbalanced_enter_exit_is_rejected():
    assert isinstance(build_trace(_events(f"E {INNER} {CALL_SITE}\nX 1 {OUTER} {CALL_SITE}\n")), str)


def test_probe_plan_and_candidates_follow_the_allocation_stack():
    record = _trace(TWO_FRAMES)
    [allocation] = leaked_allocations(record, frozenset({int(POINTER, 16)}))
    assert candidate_exits(record, allocation) == [1, 2] and probe_plan(record, [allocation]) == [1, 2]


def test_allocations_made_outside_instrumented_code_are_not_traced():
    record = _trace(f"A {POINTER} 6 {ALLOC_SITE}\n")
    assert leaked_allocations(record, frozenset({int(POINTER, 16)})) == []


def test_release_sites_come_from_frees_of_the_same_allocation_site():
    passing = _trace(f"E {INNER} {CALL_SITE}\nA {POINTER} 6 {ALLOC_SITE}\nX 1 {INNER} {CALL_SITE}\nF {POINTER} {FREE_SITE}\n")
    other = _trace("A 0x502000000070 6 0x999\nF 0x502000000070 0x888\n")
    assert release_sites([passing, other], int(ALLOC_SITE, 16)) == [int(FREE_SITE, 16)]


def test_probe_result_zero_ignores_reports_printed_on_stderr():
    events = _events("E 0x1 0x2\nX 1 0x1 0x2\nC 1 in 0\n")
    probes = leaked_objects_per_probe(f"LEANC-OWNERSHIP-CHECK 1 in\n{REPORT}\n", events)
    assert probes == {ProbeKey(1, "in"): frozenset()}


def test_only_the_first_report_after_a_marker_belongs_to_the_probe():
    events = _events("E 0x1 0x2\nX 1 0x1 0x2\nC 1 in 1\n")
    later = REPORT.replace(POINTER, "0x502000000090") + "\n"
    probes = leaked_objects_per_probe(f"LEANC-OWNERSHIP-CHECK 1 in\n{REPORT}\n{later}", events)
    assert probes == {ProbeKey(1, "in"): frozenset({int(POINTER, 16)})}


@pytest.mark.parametrize(
    "stderr",
    ["LEANC-OWNERSHIP-CHECK 2 in\n", "", "LEANC-OWNERSHIP-CHECK 1 in\nLEANC-OWNERSHIP-CHECK 1 in\n", "LEANC-OWNERSHIP-CHECK 1 in\n0x1 (1 bytes)\n"],
)
def test_forged_or_inconsistent_probe_output_is_rejected(stderr):
    events = _events("E 0x1 0x2\nX 1 0x1 0x2\nC 1 in 1\n")
    assert isinstance(leaked_objects_per_probe(stderr, events), str)


def test_recording_run_must_not_contain_probe_markers():
    assert leaked_objects_at_exit(REPORT) == frozenset({int(POINTER, 16)})
    assert isinstance(leaked_objects_at_exit("LEANC-OWNERSHIP-CHECK 1 in\n"), str)


SYMBOLIZER_JSON = json.dumps([
    {"Address": "0x10683b", "Symbol": [{"FileName": "/s/snapshot/src/join.c", "FunctionName": "join_with_sep", "Line": 22, "Column": 17,
                                        "StartFileName": "/s/snapshot/src/join.c", "StartLine": 5}]},
    {"Address": "0x1", "Symbol": [{"FileName": "/usr/include/x.h", "FunctionName": "x", "Line": 3, "Column": 0}]},
])


def test_symbolizer_output_maps_to_project_locations_only():
    addresses = [CodeAddress(0x10683C, True), CodeAddress(0x1, False)]
    result = parse_symbolizer_output(SYMBOLIZER_JSON, addresses, {"src/join.c"}, "/s/snapshot")
    assert result.problem is None
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


TRACE = MemoryOwnershipTrace(
    test_case="separator_in_field",
    allocation=Location(file="src/join.c", line=22, function="join_with_sep"),
    released_at=[Location(file="tests/test_join.c", line=28, function="expect_ok")],
    loss=LOSS_INSIDE_FUNCTION,
    function=Location(file="src/join.c", line=5, function="join_with_sep"),
    returned_to=Location(file="tests/test_join.c", line=38, function="expect_err"),
)


def _failure(trace=None) -> FailureReport:
    return FailureReport(
        id="sanitizer:separator_in_field:lsan:memory-leak",
        kind="runtime",
        tool="lsan",
        property_id="memory-leak",
        description="LeakSanitizer: Direct leak of 6 byte(s)",
        location=Location(file="src/join.c", line=22, function="join_with_sep"),
        inputs=[CounterexampleInput(name="test_case", value="separator_in_field")],
        ownership_trace=trace,
    )


def test_prompt_shows_the_labelled_trace_without_addresses():
    text = render_failure(_failure(TRACE))
    assert OWNERSHIP_TRACE_LABEL in text and "released at tests/test_join.c:28" in text and "0x" not in text
    assert render_failure(_failure(TRACE)) == text
    assert OWNERSHIP_TRACE_LABEL not in render_failure(_failure())


def test_json_summary_entry_carries_the_trace():
    assert _failure_entry("sanitizer", _failure(TRACE))["ownership_trace"]["loss"] == LOSS_INSIDE_FUNCTION
    assert _failure_entry("sanitizer", _failure())["ownership_trace"] is None


def test_leak_failures_are_grouped_by_test_case():
    other = _failure().model_copy(update={"tool": "asan", "property_id": "heap-buffer-overflow"})
    assert list(leak_failures_by_case([_failure(), other])) == ["separator_in_field"]


def test_trace_log_reader_refuses_symlinks_fifos_and_oversized_files(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("A 0x1 1 0x1\n")
    link = tmp_path / "link.log"
    link.symlink_to(secret)
    fifo = tmp_path / "fifo.log"
    os.mkfifo(fifo)
    big = tmp_path / "big.log"
    big.write_bytes(b"A" * (8 * 1024 * 1024 + 1))
    for path in (link, fifo, big, tmp_path / "missing.log"):
        assert _read_trace_log(path).problem is not None
    assert _read_trace_log(secret).text == "A 0x1 1 0x1\n"
