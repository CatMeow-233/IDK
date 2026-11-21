"""Safe external-process execution.

Rules enforced here (see docs/security.md):

* Commands are always an argv list; ``shell=True`` is never used.
* The child gets a *minimal* environment (no inherited secrets such as API keys).
* Wall-clock timeout, CPU / address-space / file-size / core rlimits.
* stdout / stderr are read incrementally and capped; exceeding the cap kills the
  process group and marks the output as truncated.
* The child runs in its own session so the whole process group can be killed.
* Optionally the argv is wrapped by a :class:`~leanc.foundation.sandbox.Sandbox`.
"""

from __future__ import annotations

import os
import resource
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from leanc.foundation.models import ToolInvocation
from leanc.foundation.redact import redact
from leanc.foundation.sandbox import Sandbox, SandboxMounts

DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass(frozen=True)
class Limits:
    timeout_s: float = 60.0
    cpu_s: int | None = None  # RLIMIT_CPU (seconds)
    memory_mb: int | None = None  # RLIMIT_AS; do NOT use with ASan (huge shadow mappings)
    max_output_bytes: int = 4 * 1024 * 1024  # per stream
    max_file_mb: int | None = 512  # RLIMIT_FSIZE
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
    sandbox: str = "none"
    error: str | None = None  # failure to launch (e.g. executable missing)
    display_argv: list[str] = field(default_factory=list)  # argv before sandbox wrapping

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
        """Convert to a :class:`ToolInvocation`, writing (redacted) stdout/stderr
        into ``run_dir/logs/<log_name>.{stdout,stderr}.txt`` when given."""
        out_rel = err_rel = None
        if run_dir is not None and log_name:
            logs = Path(run_dir) / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            out_p = logs / f"{log_name}.stdout.txt"
            err_p = logs / f"{log_name}.stderr.txt"
            out_p.write_text(redact(self.stdout), encoding="utf-8")
            err_p.write_text(redact(self.stderr), encoding="utf-8")
            out_rel = str(out_p.relative_to(run_dir))
            err_rel = str(err_p.relative_to(run_dir))
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


def minimal_env(extra: Mapping[str, str] | None = None, home: str | None = None) -> dict[str, str]:
    """A small, explicit environment.  Nothing is inherited from the parent
    process, so credentials in the parent environment never reach tools or
    untrusted programs."""
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


def _make_preexec(limits: Limits):
    def _preexec() -> None:  # runs in the child between fork and exec
        try:
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except (ValueError, OSError):
            pass
        if limits.cpu_s is not None:
            cpu = int(limits.cpu_s)
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        if limits.memory_mb is not None:
            mem = int(limits.memory_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        if limits.max_file_mb is not None:
            fs = int(limits.max_file_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_FSIZE, (fs, fs))
        if limits.max_open_files is not None:
            n = int(limits.max_open_files)
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (n, n))
            except (ValueError, OSError):
                pass

    return _preexec


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


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
    """Run ``argv`` (never through a shell) and capture bounded output."""
    if isinstance(argv, (str, bytes)):
        raise TypeError("argv must be a sequence of arguments, not a string (no shell execution)")
    argv = [str(a) for a in argv]
    if not argv:
        raise ValueError("empty argv")
    for a in argv:
        if "\x00" in a:
            raise ValueError("NUL byte in argument")
    limits = limits or Limits()
    cwd = str(cwd)
    child_env = dict(env) if env is not None else minimal_env()

    display_argv = list(argv)
    sandbox_kind = "none"
    exec_argv = list(argv)
    if sandbox is not None and sandbox.kind != "none":
        exec_argv = sandbox.wrap(argv, cwd=cwd, env=child_env, mounts=mounts)
        sandbox_kind = sandbox.kind
        # bwrap clears and sets the environment itself; keep the outer env minimal.
        child_env = minimal_env()

    start = time.monotonic()
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv list, shell=False by construction
            exec_argv,
            cwd=cwd,
            env=child_env,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
            preexec_fn=_make_preexec(limits),
            close_fds=True,
        )
    # SubprocessError: the preexec hook failed in the child (e.g. an rlimit
    # above the inherited hard limit); that is a launch failure, not a crash.
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return ProcResult(
            argv=exec_argv,
            cwd=cwd,
            exit_code=None,
            signal=None,
            timed_out=False,
            output_truncated=False,
            duration_s=time.monotonic() - start,
            stdout="",
            stderr="",
            sandbox=sandbox_kind,
            error=f"failed to launch {argv[0]!r}: {exc}",
            display_argv=display_argv,
        )

    bufs: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = False
    timed_out = False
    sel = selectors.DefaultSelector()
    assert proc.stdout is not None and proc.stderr is not None
    sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
    sel.register(proc.stderr, selectors.EVENT_READ, "stderr")
    # stdin is fed from the same select loop with non-blocking writes.  A
    # blocking write would hang forever (defeating the timeout) when the child
    # never reads its input and the data exceeds the pipe buffer.
    stdin_view = memoryview(stdin_data) if stdin_data is not None else None
    stdin_pos = 0
    if proc.stdin is not None:
        if stdin_view is not None and len(stdin_view) > 0:
            os.set_blocking(proc.stdin.fileno(), False)
            sel.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            proc.stdin.close()

    def _close_stdin() -> None:
        if proc.stdin is not None and not proc.stdin.closed:
            try:
                sel.unregister(proc.stdin)
            except (KeyError, ValueError):
                pass
            try:
                proc.stdin.close()
            except OSError:
                pass
    deadline = start + float(limits.timeout_s)
    open_streams = 2
    while open_streams:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _kill_group(proc)
            break
        for key, _ in sel.select(timeout=min(remaining, 0.25)):
            if key.data == "stdin":
                assert stdin_view is not None
                fd = key.fileobj.fileno()  # type: ignore[union-attr]
                try:
                    stdin_pos += os.write(fd, stdin_view[stdin_pos : stdin_pos + 65536])
                except BlockingIOError:
                    continue
                except OSError:  # BrokenPipeError: the child closed its stdin
                    stdin_pos = len(stdin_view)
                if stdin_pos >= len(stdin_view):
                    _close_stdin()
                continue
            chunk = os.read(key.fileobj.fileno(), 65536)  # type: ignore[union-attr]
            if not chunk:
                sel.unregister(key.fileobj)
                open_streams -= 1
                continue
            buf = bufs[key.data]
            room = limits.max_output_bytes - len(buf)
            if room > 0:
                buf.extend(chunk[:room])
            if len(chunk) > room:
                truncated = True
        if truncated:
            _kill_group(proc)
            break
    _close_stdin()
    sel.close()
    try:
        proc.wait(timeout=max(0.1, deadline - time.monotonic()) if not (timed_out or truncated) else 5)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        proc.wait(timeout=5)
    # Make sure nothing in the group survives (e.g. grandchildren holding pipes).
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    for stream in (proc.stdout, proc.stderr):
        try:
            stream.close()
        except OSError:
            pass
    rc = proc.returncode
    sig = -rc if rc is not None and rc < 0 else None
    return ProcResult(
        argv=exec_argv,
        cwd=cwd,
        exit_code=rc if rc is not None and rc >= 0 else None,
        signal=sig,
        timed_out=timed_out,
        output_truncated=truncated,
        duration_s=time.monotonic() - start,
        stdout=bufs["stdout"].decode("utf-8", errors="replace"),
        stderr=bufs["stderr"].decode("utf-8", errors="replace"),
        sandbox=sandbox_kind,
        display_argv=display_argv,
    )
