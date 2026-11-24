from __future__ import annotations

import json
from pathlib import Path

from cbmc_replay_support import FRONTEND_PASSTHROUGH, VACUITY_FLAG, VACUITY_RUN_TEST, fixture_messages, write_executable
from cbmc_support import _fake_tools, make_cbmc_project

from leanc.backends.cbmc import execution as cbmc_execution
from leanc.backends.cbmc import run_cbmc
from leanc.backends.cbmc.preprocess import CBMC_BANNER_PREFIX, PREPROCESS_FLAG
from leanc.backends.cbmc.vacuity_divergence import DIVERGENCE_RULE
from leanc.foundation.models import StageResult
from leanc.foundation.runner import ProcResult
from leanc.foundation.status import Outcome, Status
from leanc.toolchain.context import BackendContext

PASSING_PAYLOAD = "arr_get_ok.verify.json"
VACUITY_PAYLOAD = "arr_get_ok.vacuity.json"
BANNER = "CBMC version 5.95.1 (cbmc-5.95.1) 64-bit"
SMALL_OUTPUT_LIMIT = 50_000
OVERSIZED_OUTPUT_BYTES = 60_000
START_ERROR = "No such file or directory: cbmc"
EDITABLE_PREPROCESS = "cbmc --preprocess of src/impl.c"


def _fake_cbmc(tmp_path: Path, preprocess_action: str) -> str:
    directory = tmp_path / "fake"
    directory.mkdir()
    verify, vacuity = directory / "verify.json", directory / "vacuity.json"
    verify.write_text(json.dumps(fixture_messages(PASSING_PAYLOAD)))
    vacuity.write_text(json.dumps(fixture_messages(VACUITY_PAYLOAD)))
    return write_executable(
        directory / "cbmc",
        'for a in "$@"; do last="$a"; done\n'
        f'for a in "$@"; do [ "$a" = "{PREPROCESS_FLAG}" ] && {{ {preprocess_action}; }}; done\n'
        f"{FRONTEND_PASSTHROUGH}{VACUITY_RUN_TEST}"
        f'if [ -n "$VAC" ]; then cat {vacuity}; else cat {verify}; fi',
    )


def _context(tmp_path: Path, preprocess_action: str, **verification) -> BackendContext:
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get", **verification)
    _fake_tools(ctx, cbmc=_fake_cbmc(tmp_path, preprocess_action))
    return ctx


def _without_vacuity_run(result: StageResult) -> bool:
    return "vacuity" not in result.details["argv"] and result.details["vacuity"]["status"] == "skipped"


def test_the_passthrough_control_passes(tmp_path):
    result = run_cbmc(_context(tmp_path, f'echo "{BANNER}"; cat "$last"; exit 0'))
    assert result.outcome is Outcome.PASS, result.summary


def test_a_failing_preprocess_is_an_error_without_a_vacuity_run(tmp_path):
    result = run_cbmc(_context(tmp_path, 'echo "fatal: cannot open source" >&2; exit 1'))
    assert result.outcome is Outcome.ERROR and result.status is Status.TOOL_ERROR, result.summary
    assert f"{EDITABLE_PREPROCESS} failed with exit code 1: fatal: cannot open source" in result.summary
    assert _without_vacuity_run(result)


def test_a_preprocess_output_without_the_banner_is_an_error(tmp_path):
    result = run_cbmc(_context(tmp_path, 'cat "$last"; exit 0'))
    assert result.outcome is Outcome.ERROR, result.summary
    assert f"{EDITABLE_PREPROCESS} did not start its output with a '{CBMC_BANNER_PREFIX}' line" in result.summary
    assert _without_vacuity_run(result)


def test_a_preprocess_that_times_out_is_a_timeout(tmp_path):
    result = run_cbmc(_context(tmp_path, "exec sleep 30", timeout_s=1))
    assert result.outcome is Outcome.TIMEOUT and result.status is Status.TIMEOUT, result.summary
    assert f"{EDITABLE_PREPROCESS} timed out" in result.summary
    assert result.invocations[-1].timed_out and _without_vacuity_run(result)


def test_a_truncated_preprocess_output_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cbmc_execution, "MAX_OUTPUT_BYTES", SMALL_OUTPUT_LIMIT)
    result = run_cbmc(_context(tmp_path, f'echo "{BANNER}"; head -c {OVERSIZED_OUTPUT_BYTES} /dev/zero | tr "\\0" x; exit 0'))
    assert result.outcome is Outcome.ERROR, result.summary
    assert f"{EDITABLE_PREPROCESS} output exceeded {SMALL_OUTPUT_LIMIT} bytes" in result.summary
    assert _without_vacuity_run(result)


def test_a_preprocess_that_cannot_start_is_an_error(tmp_path, monkeypatch):
    ctx = _context(tmp_path, f'echo "{BANNER}"; cat "$last"; exit 0')
    run = BackendContext.run

    def unstartable_preprocessor(self, argv, **kwargs):
        if PREPROCESS_FLAG not in argv:
            return run(self, argv, **kwargs)
        return ProcResult(argv, str(kwargs.get("cwd", "")), None, None, False, False, 0.0, "", "", error=START_ERROR)

    monkeypatch.setattr(BackendContext, "run", unstartable_preprocessor)
    result = run_cbmc(ctx)
    assert result.outcome is Outcome.ERROR, result.summary
    assert f"{EDITABLE_PREPROCESS} could not be started: {START_ERROR}" in result.summary
    assert _without_vacuity_run(result)


def test_an_extra_line_in_the_preprocessed_text_with_the_define_is_an_error(tmp_path):
    action = (
        f'for b in "$@"; do [ "$b" = "{VACUITY_FLAG}" ] && {{ echo "{BANNER}"; echo "int extra;"; cat "$last"; exit 0; }}; '
        f'done; echo "{BANNER}"; cat "$last"; exit 0'
    )
    result = run_cbmc(_context(tmp_path, action))
    assert result.outcome is Outcome.ERROR, result.summary
    assert f"the editable source src/impl.c {DIVERGENCE_RULE}: preprocessed line 1:" in result.summary
    assert _without_vacuity_run(result)
