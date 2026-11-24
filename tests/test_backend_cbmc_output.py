from __future__ import annotations

import json
import stat
from pathlib import Path

from cbmc_replay_support import DIVERGENCE_PREPROCESS_RUNS, FRONTEND_PASSTHROUGH, SYMBOL_TABLE_RUNS, VACUITY_RUN_TEST
from cbmc_support import FIX, _fake_tools, make_cbmc_project

from leanc.backends.cbmc import (
    coverage_by_file,
    interpret_output,
    parse_cbmc_json,
    run_cbmc,
)
from leanc.foundation.status import Outcome, Status


def _fake_cbmc(tmp_path: Path, script: str, payload: str | None = None) -> str:
    d = tmp_path / "fakecbmc"
    d.mkdir(exist_ok=True)
    if payload is not None:
        (d / "payload.json").write_text(payload)
    exe = d / "cbmc"
    exe.write_text("#!/bin/sh\n" + FRONTEND_PASSTHROUGH + script.replace("@PAYLOAD@", str(d / "payload.json")) + "\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(exe)


def test_out_of_memory_is_error(tmp_path):
    payload = (FIX / "json" / "mul_div_oom.verify.json").read_text()
    ctx = make_cbmc_project(tmp_path, src="mul_div", harness="mul_div_harness", function="mul_div")
    _fake_tools(ctx, cbmc=_fake_cbmc(tmp_path, "cat @PAYLOAD@\nexit 6", payload))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR, res.summary
    assert "ran out of memory" in res.summary and "memory_mb=4096" in res.summary
    assert "could not decide 2 property(ies)" in res.summary
    assert res.diagnostics and res.diagnostics[0].rule_id == "out-of-memory"
    assert res.failures == []


def test_out_of_memory_without_results_is_error(tmp_path):
    data = json.loads((FIX / "json" / "mul_div_oom.verify.json").read_text())
    data = [m for m in data if "result" not in m]
    ctx = make_cbmc_project(tmp_path, src="mul_div", harness="mul_div_harness", function="mul_div")
    _fake_tools(ctx, cbmc=_fake_cbmc(tmp_path, "cat @PAYLOAD@\nexit 6", json.dumps(data)))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR
    assert res.summary.startswith("CBMC (verify run) ran out of memory (verification.memory_mb=4096)")
    assert "SAT checker ran out of memory" in res.summary


def test_counterexample_found_before_running_out_of_memory_is_fail(tmp_path):
    data = json.loads((FIX / "json" / "mul_div_oom.verify.json").read_text())
    avg = json.loads((FIX / "json" / "avg.verify.json").read_text())
    prop_result = next(m["result"] for m in avg if "result" in m)[0]
    assert prop_result["status"] == "FAILURE"
    for m in data:
        if "result" in m:
            m["result"].append(prop_result)
    ctx = make_cbmc_project(tmp_path, src="mul_div", harness="mul_div_harness", function="mul_div")
    _fake_tools(ctx, cbmc=_fake_cbmc(tmp_path, "cat @PAYLOAD@\nexit 6", json.dumps(data)))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.FAIL
    assert [f.property_id for f in res.failures] == [prop_result["property"]]
    assert "2 property(ies) could not be decided (CBMC ran out of memory" in res.summary


def test_undecided_properties_are_error(tmp_path):
    data = json.loads((FIX / "json" / "mul_div_oom.verify.json").read_text())
    data = [m for m in data if m.get("messageText") != "SAT checker ran out of memory"]
    ctx = make_cbmc_project(tmp_path, src="mul_div", harness="mul_div_harness", function="mul_div")
    _fake_tools(ctx, cbmc=_fake_cbmc(tmp_path, "cat @PAYLOAD@\nexit 6", json.dumps(data)))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR
    assert "could not decide 2 property(ies)" in res.summary and "main.assertion.1 (ERROR)" in res.summary


def test_killed_by_signal_is_error(tmp_path):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    _fake_tools(ctx, cbmc=_fake_cbmc(tmp_path, 'echo \'[{"program": "CBMC 5.95.1 (cbmc-5.95.1)"}\'\nkill -SEGV $$'))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR
    assert "killed by SIGSEGV" in res.summary and "possibly out of memory" in res.summary


def test_garbage_output_is_error(tmp_path):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    _fake_tools(ctx, cbmc=_fake_cbmc(tmp_path, "echo 'this is not json'\necho 'boom' >&2\nexit 0"))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR
    assert "unparsable JSON" in res.summary and "boom" in res.summary


def _fake_cbmc_by_mode(tmp_path: Path, verify_payload: str, vacuity_action: str, vacuity_payload: str = "") -> str:
    d = tmp_path / "fakecbmc"
    d.mkdir(exist_ok=True)
    (d / "verify.json").write_text(verify_payload)
    (d / "vacuity.json").write_text(vacuity_payload)
    exe = d / "cbmc"
    exe.write_text(
        "#!/bin/sh\n"
        + FRONTEND_PASSTHROUGH
        + VACUITY_RUN_TEST
        + f'if [ -n "$VAC" ]; then {vacuity_action.replace("@VACUITY@", str(d / "vacuity.json"))}; '
        f'else cat {d / "verify.json"}; fi\n'
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(exe)


def test_replayed_vacuity_outputs(tmp_path):
    ok = (FIX / "json" / "arr_get_ok.verify.json").read_text()
    for vac_file, outcome in (
        ("arr_get_ok.vacuity.json", Outcome.PASS),
        ("arr_get_contradictory.vacuity.json", Outcome.INCONCLUSIVE),
    ):
        sub = tmp_path / vac_file
        sub.mkdir()
        ctx = make_cbmc_project(sub, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
        vac = (FIX / "json" / vac_file).read_text()
        _fake_tools(ctx, cbmc=_fake_cbmc_by_mode(sub, ok, "cat @VACUITY@", vac))
        res = run_cbmc(ctx)
        assert res.outcome is outcome, (vac_file, res.summary)
    assert res.details["vacuity"]["probes"] == {"after_call": False, "in_range": False, "out_of_range": False}
    reported = {f.id: f.description for f in res.failures}
    assert "not reported by CBMC" in reported["cbmc:vacuity:in_range"]
    assert "not reported by CBMC" not in reported["cbmc:vacuity:after_call"]


def test_vacuity_run_timeout_is_timeout(tmp_path):
    ok = (FIX / "json" / "arr_get_ok.verify.json").read_text()
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get", timeout_s=1)
    _fake_tools(ctx, cbmc=_fake_cbmc_by_mode(tmp_path, ok, "exec sleep 30"))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.TIMEOUT and res.status is Status.TIMEOUT
    assert "vacuity run" in res.summary and "timed out" in res.summary
    assert [i.timed_out for i in res.invocations] == [False] * (DIVERGENCE_PREPROCESS_RUNS + 1 + SYMBOL_TABLE_RUNS) + [True]
    assert res.details["vacuity"]["status"] == "skipped"


def test_vacuity_run_garbage_is_error(tmp_path):
    ok = (FIX / "json" / "arr_get_ok.verify.json").read_text()
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    _fake_tools(ctx, cbmc=_fake_cbmc_by_mode(tmp_path, ok, "echo nope"))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.ERROR
    assert "vacuity run" in res.summary and "unparsable JSON" in res.summary


def test_replayed_failure_output_is_fail(tmp_path):
    payload = (FIX / "json" / "arr_get_off_by_one.verify.json").read_text()
    ctx = make_cbmc_project(tmp_path, src="arr_get_off_by_one", harness="arr_get_harness", function="arr_get")
    _fake_tools(ctx, cbmc=_fake_cbmc(tmp_path, "cat @PAYLOAD@\nexit 10", payload))
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.FAIL
    assert {f.property_id for f in res.failures} == {
        "main.assertion.3",
        "main.assertion.4",
        "arr_get.pointer_dereference.11",
    }


def test_parse_cbmc_json_salvages_truncated_output():
    text = (FIX / "json" / "syntax_error.verify.json").read_text()
    msgs, err = parse_cbmc_json(text)
    assert err is None and msgs
    cut = text[: text.index("PARSING ERROR") + 20]
    msgs2, err2 = parse_cbmc_json(cut)
    assert err2 and "invalid JSON" in err2
    assert any("syntax error" in str(m.get("messageText", "")) for m in msgs2)
    assert parse_cbmc_json("") == ([], "empty output")
    assert parse_cbmc_json('{"a": 1}')[1].startswith("unexpected JSON")


def test_interpret_output_fields():
    out = interpret_output((FIX / "json" / "arr_get_ok.verify.json").read_text())
    assert out.parse_error is None and out.cprover_status == "success"
    assert out.program.startswith("CBMC 5.")
    assert out.platform and "64-bit" in out.platform
    assert out.results and all(r["status"] == "SUCCESS" for r in out.results)
    assert out.errors == []
    syn = interpret_output((FIX / "json" / "syntax_error.verify.json").read_text())
    assert syn.results is None and "PARSING ERROR" in syn.error_texts()


def test_coverage_by_file_on_captured_goals():
    out = interpret_output((FIX / "json" / "count_zeros_dead_code.coverage.json").read_text())
    assert out.goals
    cov = coverage_by_file(out.goals, ["src/impl.c", "src/other.c"])
    assert cov["src/other.c"] == {"goals": 0, "covered": 0, "uncovered_lines": []}
    st = cov["src/impl.c"]
    assert st["goals"] == len([g for g in out.goals if g["sourceLocation"]["file"] == "src/impl.c"])
    assert st["uncovered_lines"] == [8]
