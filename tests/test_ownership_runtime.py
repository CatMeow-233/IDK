from __future__ import annotations

import re
import resource
import signal
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from sanitizer_support import make_project, make_spec

from leanc.backends.sanitizer.ownership_analysis import build_trace
from leanc.backends.sanitizer.ownership_build import build_trace_binary
from leanc.backends.sanitizer.ownership_log import (
    LOG_LINE_END,
    LOG_WRITE_FAILED,
    LOG_WRITE_FAILED_MARKER,
    TRUNCATED_LOG,
    leaked_objects_per_probe,
    log_write_problem,
    parse_events,
)
from leanc.backends.sanitizer.ownership_replay import replay_env
from leanc.backends.sanitizer.plan import sanitizer_env, sanitizer_plan
from leanc.foundation.runner import minimal_env
from leanc.toolchain.context import make_context

pytestmark = pytest.mark.usefixtures("needs_clang")

LEAK_CASE = {"name": "huge_realloc", "args": ["10"], "expect_stdout": "r=42\n"}
FULL_DEVICE = Path("/dev/full")
RUN_TIMEOUT_S = 60
MEMORY_MB = 1024
PROBED_EXITS = [1, 2]
PARTIAL_STDERR = "partial "
BYTES_INTO_SECOND_LINE = 5
FIRST_EXIT_LINE_START = LOG_LINE_END + "X 1 "
FIRST_PROBED_EXIT = [1]
BYTES_INTO_FIRST_EXIT_LINE = 3
TWENTY_DIGIT_FAILED_REALLOC_RE = re.compile(r"^R (0x[0-9a-f]+) 0x0 18446744073709551615 0x[0-9a-f]+$", re.MULTILINE)
PLAN = sanitizer_plan(make_spec([LEAK_CASE]).tests.sanitizers)

HUGE_REALLOC_SRC = textwrap.dedent(
    """\
    #include <stdio.h>
    #include <stdlib.h>
    #include "f.h"

    __attribute__((noinline)) static char *grow(char *p, size_t n)
    {
        return realloc(p, n);
    }

    int f(int n)
    {
        char *p = malloc(16);
        if (!p) return -1;
        if (grow(p, (size_t)0 - 1) != NULL) return -2;
        fputs("partial ", stderr);
        return n + 32;
    }
    """
)


@pytest.fixture(scope="module")
def trace_binary(tmp_path_factory) -> Path:
    root = make_project(tmp_path_factory.mktemp("ownership_runtime") / "proj", HUGE_REALLOC_SRC)
    ctx = make_context(make_spec([LEAK_CASE]), root, root / ".leanc" / "runs" / "r1")
    build = build_trace_binary(ctx, PLAN, Path(ctx.build_dir) / "ownership")
    assert build.binary is not None, build.problem
    return build.binary


def _ignore_file_size_signal(limit: int):
    def _apply() -> None:
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

    return _apply


def _run(binary: Path, log: Path, probed: list[int] | None = None, file_size_limit: int | None = None):
    inputs = SimpleNamespace(env=sanitizer_env(PLAN, MEMORY_MB, None))
    env = minimal_env(replay_env(inputs, log, probed))
    preexec = _ignore_file_size_signal(file_size_limit) if file_size_limit is not None else None
    proc = subprocess.run(
        [str(binary), *LEAK_CASE["args"]],
        cwd=binary.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_S,
        preexec_fn=preexec,
        restore_signals=file_size_limit is None,
    )
    return proc.stderr, log.read_text() if log.is_file() else ""


def _first_exit_line_span(log: str) -> tuple[int, int]:
    start = log.index(FIRST_EXIT_LINE_START) + len(LOG_LINE_END)
    return start, log.index(LOG_LINE_END, start) + len(LOG_LINE_END)


def test_failed_realloc_of_a_twenty_digit_size_is_a_parsed_event(trace_binary, tmp_path):
    _, log = _run(trace_binary, tmp_path / "record.log")
    failed = TWENTY_DIGIT_FAILED_REALLOC_RE.search(log)
    assert failed, log
    events = parse_events(log)
    assert not isinstance(events, str), events
    run = build_trace(events)
    assert not isinstance(run, str), run
    assert int(failed.group(1), 16) in run.live


def test_probe_markers_start_a_line_after_partial_stderr_output(trace_binary, tmp_path):
    stderr, log = _run(trace_binary, tmp_path / "probe.log", PROBED_EXITS)
    assert PARTIAL_STDERR in stderr
    events = parse_events(log)
    assert not isinstance(events, str), events
    probes = leaked_objects_per_probe(stderr, events)
    assert isinstance(probes, dict), (probes, stderr)
    assert list(probes) == [e.probe for e in events if e.kind == "C"]


def test_log_write_failure_on_the_first_write_prints_one_marker(trace_binary, tmp_path):
    stderr, _ = _run(trace_binary, FULL_DEVICE)
    assert stderr.splitlines().count(LOG_WRITE_FAILED_MARKER) == 1, stderr
    assert log_write_problem(stderr) == LOG_WRITE_FAILED


def test_log_cut_inside_a_line_is_rejected_and_reported(trace_binary, tmp_path):
    _, complete = _run(trace_binary, tmp_path / "complete.log")
    first_line = complete.split("\n", 1)[0]
    limit = len(first_line) + 1 + BYTES_INTO_SECOND_LINE
    stderr, cut = _run(trace_binary, tmp_path / "cut.log", file_size_limit=limit)
    assert len(cut) == limit and cut.startswith(first_line + "\n"), cut
    assert parse_events(cut) == TRUNCATED_LOG
    assert stderr.splitlines().count(LOG_WRITE_FAILED_MARKER) == 1, stderr


def test_write_failure_after_a_partial_stderr_line_is_still_reported(trace_binary, tmp_path):
    _, complete = _run(trace_binary, tmp_path / "complete.log")
    _, limit = _first_exit_line_span(complete)
    stderr, cut = _run(trace_binary, tmp_path / "cut.log", file_size_limit=limit)
    assert len(cut) == limit and complete.startswith(cut), cut
    assert not isinstance(parse_events(cut), str), cut
    assert log_write_problem(stderr) == LOG_WRITE_FAILED, stderr
    assert PARTIAL_STDERR + LOG_LINE_END + LOG_WRITE_FAILED_MARKER + LOG_LINE_END in stderr, stderr


def test_no_log_write_follows_a_failed_write_inside_a_probed_exit(trace_binary, tmp_path):
    _, complete = _run(trace_binary, tmp_path / "complete.log", FIRST_PROBED_EXIT)
    start, _ = _first_exit_line_span(complete)
    limit = start + BYTES_INTO_FIRST_EXIT_LINE
    stderr, cut = _run(trace_binary, tmp_path / "cut.log", FIRST_PROBED_EXIT, file_size_limit=limit)
    assert len(cut) == limit and complete.startswith(cut), cut
    assert stderr.splitlines().count(LOG_WRITE_FAILED_MARKER) == 1, stderr
