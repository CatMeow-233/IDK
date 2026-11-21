from __future__ import annotations

import os
import resource
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

from leanc.foundation.models import ToolInvocation
from leanc.foundation.redact import redact
from leanc.foundation.sandbox import Sandbox, SandboxKind, SandboxMounts

DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
MIB = 1024 * 1024
PIPE_CHUNK_BYTES = 64 * 1024
SELECT_POLL_INTERVAL_S = 0.25
MIN_REAP_WAIT_S = 0.1
KILLED_CHILD_REAP_TIMEOUT_S = 5
CPU_HARD_LIMIT_GRACE_S = 1
STDOUT, STDERR, STDIN = "stdout", "stderr", "stdin"

PREEXEC_HOOK_FAILED = subprocess.SubprocessError
LAUNCH_FAILURES = (OSError, ValueError, PREEXEC_HOOK_FAILED)


@dataclass(frozen=True)
class Limits:
    timeout_s: float = 60.0
    cpu_s: int | None = None
    memory_mb: int | None = None
    max_output_bytes: int = 4 * MIB
    max_file_mb: int | None = 512
    max_open_files: int | None = 1024


@dataclass
class ProcResult:
    argv: list[str]
    cwd: str
    exit_code: int | None
    signal: int | None
    timed_out: bool
    output_truncated: bool
    duration_s: float
    stdout: str
    stderr: str
    sandbox: SandboxKind = "none"
    error: str | None = None
    display_argv: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out and self.exit_code == 0

    def to_invocation(
        self,
        tool: str,
        run_dir: Path | None = None,
        log_name: str | None = None,
        note: str | None = None,
    ) -> ToolInvocation:
        out_rel = err_rel = None
        if run_dir is not None and log_name:
            out_rel, err_rel = self._write_redacted_logs(Path(run_dir), log_name)
        notes = [n for n in (note, self.error) if n]
        return ToolInvocation(
            tool=tool,
            argv=[redact(a) for a in (self.display_argv or self.argv)],
            cwd=self.cwd,
            exit_code=self.exit_code,
            signal=self.signal,
            timed_out=self.timed_out,
            output_truncated=self.output_truncated,
            duration_s=round(self.duration_s, 3),
            sandbox=self.sandbox,
            stdout_path=out_rel,
            stderr_path=err_rel,
            note="; ".join(notes) if notes else None,
        )

    def _write_redacted_logs(self, run_dir: Path, log_name: str) -> tuple[str, str]:
        logs = run_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        out_path = logs / f"{log_name}.stdout.txt"
        err_path = logs / f"{log_name}.stderr.txt"
        out_path.write_text(redact(self.stdout), encoding="utf-8")
        err_path.write_text(redact(self.stderr), encoding="utf-8")
        return str(out_path.relative_to(run_dir)), str(err_path.relative_to(run_dir))


def minimal_env(extra: Mapping[str, str] | None = None, home: str | None = None) -> dict[str, str]:
    env = {
        "PATH": DEFAULT_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": home or "/tmp",
        "TMPDIR": "/tmp",
    }
    if extra:
        env.update(extra)
    return env


def _within_inherited_hard_limit(limit: int, wanted: int) -> bool:
    _, hard = resource.getrlimit(limit)
    return hard == resource.RLIM_INFINITY or wanted <= hard


def _make_preexec(limits: Limits):
    def _apply_limits_in_child_before_exec() -> None:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if limits.cpu_s is not None:
            cpu = int(limits.cpu_s)
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + CPU_HARD_LIMIT_GRACE_S))
        if limits.memory_mb is not None:
            address_space = int(limits.memory_mb) * MIB
            resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
        if limits.max_file_mb is not None:
            file_size = int(limits.max_file_mb) * MIB
            resource.setrlimit(resource.RLIMIT_FSIZE, (file_size, file_size))
        if limits.max_open_files is not None:
            open_files = int(limits.max_open_files)
            if _within_inherited_hard_limit(resource.RLIMIT_NOFILE, open_files):
                resource.setrlimit(resource.RLIMIT_NOFILE, (open_files, open_files))

    return _apply_limits_in_child_before_exec


class GroupKill(Enum):
    KILLED = "killed"
    ALREADY_GONE = "already-gone"
    NOT_PERMITTED = "not-permitted"


def _kill_process_group(pgid: int) -> GroupKill:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return GroupKill.ALREADY_GONE
    except PermissionError:
        return GroupKill.NOT_PERMITTED
    return GroupKill.KILLED


def _kill_group(proc: subprocess.Popen) -> GroupKill:
    group_kill = _kill_process_group(proc.pid)
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    return group_kill


def _kill_lingering_group_members(proc: subprocess.Popen) -> GroupKill:
    return _kill_process_group(proc.pid)


class _PumpEnd(Enum):
    OUTPUTS_CLOSED = "outputs-closed"
    DEADLINE_PASSED = "deadline-passed"
    OUTPUT_CAP_EXCEEDED = "output-cap-exceeded"


class _StreamPump:
    def __init__(self, proc: subprocess.Popen, stdin_data: bytes | None, max_output_bytes: int):
        assert proc.stdout is not None and proc.stderr is not None
        self._proc = proc
        self._max_output_bytes = max_output_bytes
        self._selector = selectors.DefaultSelector()
        self._selector.register(proc.stdout, selectors.EVENT_READ, STDOUT)
        self._selector.register(proc.stderr, selectors.EVENT_READ, STDERR)
        self._open_outputs = 2
        self._stdin = memoryview(stdin_data if stdin_data is not None else b"")
        self._stdin_pos = 0
        self.output: dict[str, bytearray] = {STDOUT: bytearray(), STDERR: bytearray()}
        self.truncated = False
        self._feed_stdin_from_the_select_loop_without_blocking()

    def _feed_stdin_from_the_select_loop_without_blocking(self) -> None:
        stdin = self._proc.stdin
        if stdin is None:
            return
        if len(self._stdin) > 0:
            os.set_blocking(stdin.fileno(), False)
            self._selector.register(stdin, selectors.EVENT_WRITE, STDIN)
        else:
            stdin.close()

    def run_until(self, deadline: float) -> _PumpEnd:
        while self._open_outputs:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _PumpEnd.DEADLINE_PASSED
            for key, _ in self._selector.select(timeout=min(remaining, SELECT_POLL_INTERVAL_S)):
                if key.data == STDIN:
                    self._write_stdin_chunk(key.fd)
                else:
                    self._read_output_chunk(key)
            if self.truncated:
                return _PumpEnd.OUTPUT_CAP_EXCEEDED
        return _PumpEnd.OUTPUTS_CLOSED

    def _write_stdin_chunk(self, fd: int) -> None:
        chunk = self._stdin[self._stdin_pos : self._stdin_pos + PIPE_CHUNK_BYTES]
        try:
            self._stdin_pos += os.write(fd, chunk)
        except BlockingIOError:
            return
        except BrokenPipeError:
            self._stdin_pos = len(self._stdin)
        if self._stdin_pos >= len(self._stdin):
            self.close_stdin()

    def _read_output_chunk(self, key: selectors.SelectorKey) -> None:
        chunk = os.read(key.fd, PIPE_CHUNK_BYTES)
        if not chunk:
            self._selector.unregister(key.fileobj)
            self._open_outputs -= 1
            return
        captured = self.output[key.data]
        room = self._max_output_bytes - len(captured)
        if room > 0:
            captured.extend(chunk[:room])
        if len(chunk) > room:
            self.truncated = True

    def close_stdin(self) -> None:
        stdin = self._proc.stdin
        if stdin is None or stdin.closed:
            return
        if stdin in self._selector.get_map():
            self._selector.unregister(stdin)
        try:
            stdin.close()
        except BrokenPipeError:
            pass

    def close(self) -> None:
        self.close_stdin()
        self._selector.close()


def _validated_argv(argv: Sequence[str]) -> list[str]:
    if isinstance(argv, (str, bytes)):
        raise TypeError("argv must be a sequence of arguments, not a string (no shell execution)")
    args = [str(a) for a in argv]
    if not args:
        raise ValueError("empty argv")
    if any("\x00" in a for a in args):
        raise ValueError("NUL byte in argument")
    return args


@dataclass(frozen=True)
class _Launch:
    argv: list[str]
    env: dict[str, str]
    sandbox_kind: SandboxKind


def _plan_launch(
    argv: list[str],
    cwd: str,
    program_env: dict[str, str],
    sandbox: Sandbox | None,
    mounts: SandboxMounts | None,
) -> _Launch:
    if sandbox is None or sandbox.kind == "none":
        return _Launch(argv=list(argv), env=program_env, sandbox_kind="none")
    wrapped = sandbox.wrap(argv, cwd=cwd, env=program_env, mounts=mounts)
    return _Launch(argv=wrapped, env=minimal_env(), sandbox_kind=sandbox.kind)


def _reap_or_kill(proc: subprocess.Popen, wait_s: float) -> bool:
    try:
        proc.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        proc.wait(timeout=KILLED_CHILD_REAP_TIMEOUT_S)
        return True
    return False


def run_command(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str],
    limits: Limits | None = None,
    env: Mapping[str, str] | None = None,
    stdin_data: bytes | None = None,
    sandbox: "Sandbox | None" = None,
    mounts: "SandboxMounts | None" = None,
) -> ProcResult:
    display_argv = _validated_argv(argv)
    limits = limits or Limits()
    cwd = str(cwd)
    program_env = dict(env) if env is not None else minimal_env()
    launch = _plan_launch(display_argv, cwd, program_env, sandbox, mounts)

    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            launch.argv,
            cwd=cwd,
            env=launch.env,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
            preexec_fn=_make_preexec(limits),
            close_fds=True,
        )
    except LAUNCH_FAILURES as exc:
        return ProcResult(
            argv=launch.argv,
            cwd=cwd,
            exit_code=None,
            signal=None,
            timed_out=False,
            output_truncated=False,
            duration_s=time.monotonic() - start,
            stdout="",
            stderr="",
            sandbox=launch.sandbox_kind,
            error=f"failed to launch {display_argv[0]!r}: {exc}",
            display_argv=display_argv,
        )

    deadline = start + float(limits.timeout_s)
    pump = _StreamPump(proc, stdin_data, limits.max_output_bytes)
    pump_end = pump.run_until(deadline)
    if pump_end is not _PumpEnd.OUTPUTS_CLOSED:
        _kill_group(proc)
    pump.close()
    if pump_end is _PumpEnd.OUTPUTS_CLOSED:
        reap_wait_s = max(MIN_REAP_WAIT_S, deadline - time.monotonic())
    else:
        reap_wait_s = KILLED_CHILD_REAP_TIMEOUT_S
    reap_timed_out = _reap_or_kill(proc, reap_wait_s)
    _kill_lingering_group_members(proc)
    proc.stdout.close()
    proc.stderr.close()

    rc = proc.returncode
    return ProcResult(
        argv=launch.argv,
        cwd=cwd,
        exit_code=rc if rc is not None and rc >= 0 else None,
        signal=-rc if rc is not None and rc < 0 else None,
        timed_out=pump_end is _PumpEnd.DEADLINE_PASSED or reap_timed_out,
        output_truncated=pump.truncated,
        duration_s=time.monotonic() - start,
        stdout=pump.output[STDOUT].decode("utf-8", errors="replace"),
        stderr=pump.output[STDERR].decode("utf-8", errors="replace"),
        sandbox=launch.sandbox_kind,
        display_argv=display_argv,
    )
