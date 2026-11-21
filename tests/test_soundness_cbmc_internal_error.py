from __future__ import annotations

import json

from cbmc_replay_support import fixture_messages, replaying_cbmc, write_executable
from cbmc_support import _fake_tools, make_cbmc_project

from leanc.backends.cbmc import run_cbmc
from leanc.backends.cbmc.execution import invariant_violation_excerpt
from leanc.foundation.status import Outcome, Status

PROGRAM_LINE = 'echo \'[{"program": "CBMC 5.95.1 (cbmc-5.95.1)"}\''
CONDITION = "Condition: lhs.type() == rhs.type()"
REASON = "Reason: assignment types must match"
FILE_LINE = "File: /build/src/goto-symex/symex_assign.cpp:42 function: assign_rec"
BACKTRACE_FRAME = "cbmc(+0x4f2a1) [0x55d0c0ffee00]"
INVARIANT_REPORT = "\n".join(
    [
        "--- begin invariant violation report ---",
        "Invariant check failed",
        FILE_LINE,
        CONDITION,
        REASON,
        "Backtrace:",
        BACKTRACE_FRAME,
        "--- end invariant violation report ---",
    ]
)
ABORT = "kill -ABRT $$"
MARKER = "Invariant check failed"
PASSING_PAYLOAD = "arr_get_ok.verify.json"
VACUITY_PAYLOAD = "arr_get_ok.vacuity.json"


def _stderr_report_script(ending: str) -> str:
    return f"{PROGRAM_LINE}\ncat >&2 <<'REPORT'\n{INVARIANT_REPORT}\nREPORT\n{ending}"


def _run(tmp_path, script: str):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    _fake_tools(ctx, cbmc=write_executable(tmp_path / "fake" / "cbmc", script))
    return run_cbmc(ctx)


def _passing_payload_with_marker_description() -> list[dict]:
    messages = fixture_messages(PASSING_PAYLOAD)
    for message in messages:
        for result in message.get("result", [])[:1]:
            result["description"] = f"{MARKER}: harness text quoting a CBMC message"
    return messages


def test_invariant_excerpt_keeps_file_condition_reason_and_drops_backtrace():
    excerpt = invariant_violation_excerpt("", "noise\n" + INVARIANT_REPORT + "\nlater output")
    assert excerpt == f"{FILE_LINE}; {CONDITION}; {REASON}"
    assert invariant_violation_excerpt("Invariant check failed\nno fields") == "Invariant check failed"
    assert invariant_violation_excerpt("SAT checker ran out of memory", "") is None


def test_invariant_violation_with_sigabrt_is_internal_error(tmp_path):
    res = _run(tmp_path, _stderr_report_script(ABORT))
    assert res.outcome is Outcome.ERROR and res.status is Status.TOOL_ERROR
    assert "CBMC internal error (invariant violation, killed by SIGABRT)" in res.summary
    assert CONDITION in res.summary and REASON in res.summary and FILE_LINE in res.summary
    assert "out of memory" not in res.summary and BACKTRACE_FRAME not in res.summary
    assert [d.rule_id for d in res.diagnostics] == ["cbmc-internal-error"]


def test_invariant_violation_reported_by_exit_status_is_internal_error(tmp_path):
    res = _run(tmp_path, _stderr_report_script("exit 6"))
    assert res.outcome is Outcome.ERROR and res.status is Status.TOOL_ERROR
    assert "CBMC internal error (invariant violation)" in res.summary and CONDITION in res.summary


def test_mutation_sigabrt_without_invariant_report_keeps_memory_hint(tmp_path):
    res = _run(tmp_path, f"{PROGRAM_LINE}\n{ABORT}")
    assert res.outcome is Outcome.ERROR and res.status is Status.TOOL_ERROR
    assert "killed by SIGABRT" in res.summary and "possibly out of memory" in res.summary
    assert "internal error" not in res.summary


def test_marker_in_a_property_description_keeps_the_pass(tmp_path):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    verify = _passing_payload_with_marker_description()
    _fake_tools(ctx, cbmc=replaying_cbmc(tmp_path / "fake", verify, fixture_messages(VACUITY_PAYLOAD)))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.PASS, res.summary


def test_marker_on_stderr_beside_a_complete_verdict_keeps_the_pass(tmp_path):
    payload = tmp_path / "fake"
    payload.mkdir()
    (payload / "verify.json").write_text(json.dumps(fixture_messages(PASSING_PAYLOAD)))
    (payload / "vacuity.json").write_text(json.dumps(fixture_messages(VACUITY_PAYLOAD)))
    script = (
        "VAC=\n"
        'for a in "$@"; do [ "$a" = "-DLEANC_VACUITY_CHECK" ] && VAC=1; done\n'
        f"echo '{MARKER} (quoted by a wrapper)' >&2\n"
        f'if [ -n "$VAC" ]; then cat {payload / "vacuity.json"}; else cat {payload / "verify.json"}; fi'
    )
    res = _run(tmp_path, script)
    assert res.outcome is Outcome.PASS, res.summary
