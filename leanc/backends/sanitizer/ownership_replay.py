from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from leanc.backends.sanitizer.ownership_analysis import TraceRun, build_trace
from leanc.backends.sanitizer.ownership_build import STAGE
from leanc.backends.sanitizer.ownership_log import (
    LeakedObjects,
    ProbeKey,
    leaked_objects_at_exit,
    leaked_objects_per_probe,
    log_write_problem,
    parse_events,
)
from leanc.backends.sanitizer.plan import SanitizerPlan
from leanc.foundation.models import ToolInvocation
from leanc.foundation.redact import redact
from leanc.foundation.runner import Limits
from leanc.foundation.sandbox import SandboxMounts
from leanc.foundation.text import safe_name
from leanc.specs.model import TestCase
from leanc.toolchain.context import BackendContext

RUN_TOOL = "ownership-trace-run"
LOG_FILE = "ownership.log"
LOG_PATH_ENV = "LEANC_OWNERSHIP_LOG"
PROBED_EXITS_ENV = "LEANC_OWNERSHIP_CHECKS"
LSAN_OPTIONS = "LSAN_OPTIONS"
REPORT_OBJECTS_OPTION = "report_objects=1"
MAX_LOG_BYTES = 32 * 1024 * 1024
ASAN_OPTIONS = "ASAN_OPTIONS"
COMPACT_REPORT_OPTION = "malloc_context_size=2"
CASE_STEM_MAX_LENGTH = 60
DEFAULT_CASE_STEM = "case"


@dataclass
class OwnershipInputs:
    ctx: BackendContext
    plan: SanitizerPlan
    env: dict[str, str]
    project_files: set[str]
    case_limits: Callable[[TestCase], Limits]


@dataclass
class ReplaySession:
    inputs: OwnershipInputs
    out_root: Path
    raw_root: Path
    binary: Path
    invocations: list[ToolInvocation]
    replays_started: int = 0


@dataclass
class Replay:
    run: TraceRun | None = None
    leaked_at_exit: LeakedObjects = field(default_factory=LeakedObjects)
    probes: dict[ProbeKey, frozenset[int]] = field(default_factory=dict)
    problem: str | None = None


@dataclass(frozen=True)
class _LogRead:
    text: str = ""
    problem: str | None = None


def _read_regular_file(fd: int) -> _LogRead:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        return _LogRead(problem=f"{LOG_FILE} is not a regular file")
    chunks: list[bytes] = []
    size = 0
    while size <= MAX_LOG_BYTES:
        chunk = os.read(fd, MAX_LOG_BYTES + 1 - size)
        if not chunk:
            return _LogRead(text=b"".join(chunks).decode("utf-8", errors="replace"))
        chunks.append(chunk)
        size += len(chunk)
    return _LogRead(problem=f"{LOG_FILE} exceeds {MAX_LOG_BYTES} bytes")


def read_trace_log(path: Path) -> _LogRead:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        return _LogRead(problem=f"cannot open {LOG_FILE} ({exc.strerror})")
    try:
        return _read_regular_file(fd)
    except OSError as exc:
        return _LogRead(problem=f"cannot read {LOG_FILE} ({exc.strerror})")
    finally:
        os.close(fd)


def _with_option(env: dict[str, str], name: str, option: str) -> None:
    env[name] = f"{env[name]}:{option}" if env.get(name) else option


def replay_env(inputs: OwnershipInputs, log_path: Path, probed: list[int] | None) -> dict[str, str]:
    env = dict(inputs.env)
    _with_option(env, LSAN_OPTIONS, REPORT_OBJECTS_OPTION)
    _with_option(env, LSAN_OPTIONS, COMPACT_REPORT_OPTION)
    _with_option(env, ASAN_OPTIONS, COMPACT_REPORT_OPTION)
    env[LOG_PATH_ENV] = str(log_path)
    if probed is not None:
        env[PROBED_EXITS_ENV] = ",".join(str(o) for o in probed)
    return env


def _stem(session: ReplaySession, index: int, case: TestCase, probed: list[int] | None) -> str:
    session.replays_started += 1
    mode = "probe" if probed is not None else "record"
    name = safe_name(case.name, CASE_STEM_MAX_LENGTH) or DEFAULT_CASE_STEM
    return f"{session.replays_started:03d}-{index:02d}-{name}-{mode}"


def _run_program(session: ReplaySession, index: int, case: TestCase, probed: list[int] | None):
    ctx = session.inputs.ctx
    stem = _stem(session, index, case, probed)
    case_dir = session.out_root / "cases" / stem
    case_dir.mkdir(parents=True)
    log_path = Path(os.path.realpath(case_dir)) / LOG_FILE
    proc = ctx.run(
        [str(session.binary), *case.args],
        limits=session.inputs.case_limits(case),
        cwd=case_dir,
        env_extra=replay_env(session.inputs, log_path, probed),
        stdin_data=case.stdin.encode("utf-8") if case.stdin is not None else None,
        mounts=SandboxMounts(ro=[str(session.out_root)], rw=[str(case_dir)]),
    )
    session.invocations.append(proc.to_invocation(RUN_TOOL, ctx.run_dir, ctx.log_name(STAGE, f"ownership {stem}")))
    (session.raw_root / f"{stem}.stderr.txt").write_text(redact(proc.stderr), encoding="utf-8")
    return proc, log_path, stem


def replay(session: ReplaySession, index: int, case: TestCase, probed: list[int] | None) -> Replay:
    proc, log_path, stem = _run_program(session, index, case, probed)
    if proc.error or proc.timed_out or proc.output_truncated:
        return Replay(problem=f"replay did not complete ({proc.error or 'timeout or output limit'})")
    log = read_trace_log(log_path)
    if log.problem:
        return Replay(problem=log.problem)
    (session.raw_root / f"{stem}.log.txt").write_text(redact(log.text), encoding="utf-8")
    write_failure = log_write_problem(proc.stderr)
    if write_failure:
        return Replay(problem=write_failure)
    events = parse_events(log.text)
    if isinstance(events, str):
        return Replay(problem=events)
    run = build_trace(events)
    if isinstance(run, str):
        return Replay(problem=run)
    if probed is None:
        leaked = leaked_objects_at_exit(proc.stderr)
        return Replay(problem=leaked) if isinstance(leaked, str) else Replay(run=run, leaked_at_exit=leaked)
    probes = leaked_objects_per_probe(proc.stderr, events)
    return Replay(problem=probes) if isinstance(probes, str) else Replay(run=run, probes=probes)
