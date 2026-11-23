from __future__ import annotations

import os
import resource
import shutil
import signal
import subprocess
import textwrap
import threading
import time
from pathlib import Path

import pytest

from leanc.foundation import runner
from leanc.foundation.runner import Limits, minimal_env, run_command
from leanc.foundation.sandbox import (
    PROBE_PATH,
    NetworkIsolation,
    Sandbox,
    SandboxMounts,
    detect_sandbox,
    network_probe_blocked,
)

SH = shutil.which("sh") or "/bin/sh"
CAT = shutil.which("cat") or "/bin/cat"
PYTHON_VISIBLE_IN_SANDBOX = "/usr/bin/python3"
BWRAP_SIGNAL_EXIT_BASE = 128
PARENT_ONLY_SECRET = "sk-parent-only-0123456789abcdef"
LOOPBACK_ONLY_PROC_NET_DEV = "Inter-|   Receive\n face |bytes\n    lo:       0\n"


def _alive_and_not_zombie(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    state = stat.rsplit(")", 1)[1].split()[0]
    return state not in ("Z", "X")


def _wait_dead(pid: int, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not _alive_and_not_zombie(pid):
            return True
        time.sleep(0.05)
    return not _alive_and_not_zombie(pid)


@pytest.fixture
def sandbox(needs_bwrap):
    return detect_sandbox("auto")


def test_string_argv_is_rejected(tmp_path):
    with pytest.raises(TypeError):
        run_command("echo hi; touch pwned", cwd=tmp_path)
    with pytest.raises(TypeError):
        run_command(b"echo hi", cwd=tmp_path)
    with pytest.raises(ValueError):
        run_command([], cwd=tmp_path)
    with pytest.raises(ValueError):
        run_command(["echo", "a\x00b"], cwd=tmp_path)
    assert not (tmp_path / "pwned").exists()


def test_arguments_are_not_interpreted_by_a_shell(tmp_path):
    res = run_command(["echo", "$HOME; touch pwned", "`id`"], cwd=tmp_path)
    assert res.ok
    assert res.stdout == "$HOME; touch pwned `id`\n"
    assert not (tmp_path / "pwned").exists()


def test_timeout_kills_process_group_including_grandchild(tmp_path):
    script = "sleep 60 & echo $! > grandchild.pid; sleep 60"
    t0 = time.monotonic()
    res = run_command([SH, "-c", script], cwd=tmp_path, limits=Limits(timeout_s=1))
    elapsed = time.monotonic() - t0
    assert res.timed_out is True
    assert res.ok is False
    assert res.signal == signal.SIGKILL
    assert elapsed < 5 and res.duration_s < 5
    pid = int((tmp_path / "grandchild.pid").read_text())
    assert _wait_dead(pid), f"grandchild {pid} survived the timeout"


def test_timeout_when_child_closes_its_output_but_keeps_running(tmp_path):
    res = run_command([SH, "-c", "exec >/dev/null 2>&1; sleep 60"], cwd=tmp_path, limits=Limits(timeout_s=1))
    assert res.timed_out is True and res.duration_s < 5


def test_output_cap_truncates_and_kills(tmp_path):
    t0 = time.monotonic()
    res = run_command(["yes"], cwd=tmp_path, limits=Limits(timeout_s=30, max_output_bytes=10_000))
    assert res.output_truncated is True
    assert len(res.stdout) == 10_000
    assert res.timed_out is False
    assert res.exit_code is None and res.signal == signal.SIGKILL
    assert time.monotonic() - t0 < 10


def test_output_cap_applies_to_stderr(tmp_path):
    res = run_command([SH, "-c", "yes >&2"], cwd=tmp_path, limits=Limits(timeout_s=30, max_output_bytes=4096))
    assert res.output_truncated is True and len(res.stderr) == 4096 and res.stdout == ""


def test_output_below_cap_is_complete(tmp_path):
    res = run_command([SH, "-c", "printf 'a%.0s' $(seq 1 5000)"], cwd=tmp_path, limits=Limits(max_output_bytes=5000))
    assert res.ok and res.output_truncated is False and res.stdout == "a" * 5000


def test_secrets_in_parent_environment_are_not_visible(tmp_path, monkeypatch):
    secret = "sk-thisIsATotallySecretValue1234567890"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("LEANC_API_KEY", secret + "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret-value-123456")
    res = run_command(["env"], cwd=tmp_path)
    assert res.ok
    assert secret not in res.stdout
    for name in ("OPENAI_API_KEY", "LEANC_API_KEY", "AWS_SECRET_ACCESS_KEY"):
        assert name not in res.stdout
    keys = {line.split("=", 1)[0] for line in res.stdout.splitlines() if "=" in line}
    assert keys <= set(minimal_env()) | {"PWD", "SHLVL", "_"}
    explicit_env_only = run_command(["env"], cwd=tmp_path, env=minimal_env({"FOO": "bar"}))
    assert "FOO=bar" in explicit_env_only.stdout and secret not in explicit_env_only.stdout


def test_rlimit_cpu_stops_busy_loop(tmp_path):
    t0 = time.monotonic()
    res = run_command([SH, "-c", "while :; do :; done"], cwd=tmp_path, limits=Limits(timeout_s=30, cpu_s=1))
    assert res.timed_out is False
    assert res.signal in (signal.SIGXCPU, signal.SIGKILL)
    assert time.monotonic() - t0 < 10


def test_rlimit_fsize_limits_file_writes(tmp_path):
    res = run_command(
        [SH, "-c", "head -c 3000000 /dev/zero > big.bin"], cwd=tmp_path, limits=Limits(max_file_mb=1)
    )
    assert not res.ok
    assert (tmp_path / "big.bin").stat().st_size <= 1024 * 1024


def test_core_dumps_disabled(tmp_path):
    res = run_command([SH, "-c", "ulimit -c"], cwd=tmp_path)
    assert res.stdout.strip() == "0"


def test_open_files_limit_above_inherited_hard_limit_keeps_the_inherited_limit(tmp_path):
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard == resource.RLIM_INFINITY:
        pytest.skip("no finite inherited RLIMIT_NOFILE hard limit")
    res = run_command([SH, "-c", "ulimit -Hn"], cwd=tmp_path, limits=Limits(max_open_files=hard + 1))
    assert res.ok and res.stdout.strip() == str(hard)


def test_launch_failure_for_missing_executable(tmp_path):
    res = run_command(["/nonexistent/definitely-not-here"], cwd=tmp_path)
    assert res.error and "failed to launch" in res.error
    assert res.exit_code is None and res.ok is False and res.timed_out is False
    inv = res.to_invocation("probe", tmp_path, "missing")
    assert inv.note and "failed to launch" in inv.note
    res = run_command(["definitely-not-a-command-xyz"], cwd=tmp_path)
    assert res.error


def test_launch_failure_in_preexec_hook_is_reported_not_raised(tmp_path, monkeypatch):
    def _bad_preexec(limits):
        def _hook():
            raise ValueError("not allowed to raise maximum limit")

        return _hook

    monkeypatch.setattr(runner, "_make_preexec", _bad_preexec)
    res = run_command(["true"], cwd=tmp_path)
    assert res.error and "failed to launch" in res.error
    assert res.ok is False


def test_stdin_is_delivered_while_output_is_read(tmp_path):
    two_megabytes_echoed_while_written = os.urandom(1_000_000).hex().encode()
    res = run_command([CAT], cwd=tmp_path, stdin_data=two_megabytes_echoed_while_written, limits=Limits(timeout_s=30))
    assert res.ok and res.stdout.encode() == two_megabytes_echoed_while_written


def test_large_stdin_to_non_reading_child_does_not_defeat_timeout(tmp_path):
    box: dict = {}

    def _go():
        box["res"] = run_command(["sleep", "30"], cwd=tmp_path, stdin_data=b"x" * 4_000_000,
                                 limits=Limits(timeout_s=1))

    t0 = time.monotonic()
    th = threading.Thread(target=_go, daemon=True)
    th.start()
    th.join(15)
    assert not th.is_alive(), "run_command hung writing stdin"
    assert box["res"].timed_out is True
    assert time.monotonic() - t0 < 5


def test_stdin_to_child_that_exits_early(tmp_path):
    res = run_command(["true"], cwd=tmp_path, stdin_data=b"y" * 2_000_000, limits=Limits(timeout_s=10))
    assert res.exit_code == 0 and res.timed_out is False and res.error is None


def test_no_stdin_means_immediate_eof(tmp_path):
    res = run_command([CAT], cwd=tmp_path, limits=Limits(timeout_s=5))
    assert res.ok and res.stdout == ""
    res = run_command([CAT], cwd=tmp_path, stdin_data=b"", limits=Limits(timeout_s=5))
    assert res.ok and res.stdout == ""


def test_to_invocation_writes_redacted_logs(tmp_path, monkeypatch):
    secret = "sk-anotherSecretValueABCDEFGHIJ123"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    res = run_command(["echo", secret], cwd=tmp_path)
    inv = res.to_invocation("echo", tmp_path, "01-echo")
    out = (tmp_path / inv.stdout_path).read_text()
    assert secret not in out and "[REDACTED]" in out
    assert secret not in " ".join(inv.argv)
    assert inv.exit_code == 0 and inv.sandbox == "none"


def _sb_run(sandbox, argv, tmp_path, *, ro=None, rw=None, cwd=None, **kw):
    mounts = SandboxMounts(ro=[str(p) for p in (ro or [])], rw=[str(p) for p in (rw or [])])
    return run_command(argv, cwd=cwd or "/", sandbox=sandbox, mounts=mounts, **kw)


def test_sandbox_only_loopback_interface(sandbox, tmp_path):
    res = _sb_run(sandbox, [CAT, "/proc/net/dev"], tmp_path)
    assert res.exit_code == 0, res.stderr
    assert res.sandbox == "bwrap"
    assert res.display_argv == [CAT, "/proc/net/dev"]
    ifaces = [ln.split(":", 1)[0].strip() for ln in res.stdout.splitlines()[2:] if ":" in ln]
    assert ifaces == ["lo"]


def test_sandbox_tcp_connect_fails(sandbox, tmp_path):
    code = textwrap.dedent(
        """\
        import socket, sys
        try:
            socket.create_connection(("1.1.1.1", 80), timeout=3)
        except OSError as exc:
            print("blocked", exc.errno)
            sys.exit(0)
        print("connected")
        sys.exit(1)
        """
    )
    res = _sb_run(sandbox, [PYTHON_VISIBLE_IN_SANDBOX, "-c", code], tmp_path, limits=Limits(timeout_s=20))
    assert res.exit_code == 0, (res.stdout, res.stderr)
    assert res.stdout.startswith("blocked")


def test_sandbox_hides_home_root_and_host_tmp(sandbox, tmp_path):
    (tmp_path / "host-secret.txt").write_text("secret")
    script = (
        "for p in /home /root /opt \"$1\"; do if test -e \"$p\"; then echo \"visible $p\"; fi; done; "
        "test -e /usr/bin/sh && echo usr-ok"
    )
    res = _sb_run(sandbox, [SH, "-c", script, "sh", str(tmp_path / "host-secret.txt")], tmp_path)
    assert res.exit_code == 0, res.stderr
    assert "visible" not in res.stdout
    assert "usr-ok" in res.stdout


def test_sandbox_runs_as_nobody(sandbox, tmp_path):
    res = _sb_run(sandbox, [SH, "-c", "id -u; id -g"], tmp_path)
    assert res.exit_code == 0, res.stderr
    assert res.stdout.split() == ["65534", "65534"]


def test_sandbox_read_only_and_read_write_mounts(sandbox, tmp_path):
    ro_dir, rw_dir = tmp_path / "ro", tmp_path / "rw"
    ro_dir.mkdir()
    rw_dir.mkdir()
    (ro_dir / "input.txt").write_text("hello")
    read_then_write_ro = _sb_run(
        sandbox, [SH, "-c", 'cat "$1/input.txt"; echo x > "$1/out.txt"', "sh", str(ro_dir)], tmp_path, ro=[ro_dir]
    )
    assert read_then_write_ro.stdout == "hello"
    assert read_then_write_ro.exit_code != 0
    assert not (ro_dir / "out.txt").exists()
    res = _sb_run(sandbox, [SH, "-c", 'echo x > "$1/out.txt"', "sh", str(rw_dir)], tmp_path, rw=[rw_dir])
    assert res.exit_code == 0, res.stderr
    assert (rw_dir / "out.txt").read_text() == "x\n"


def test_sandbox_rw_subdirectory_of_ro_mount(sandbox, tmp_path):
    parent = tmp_path / "bin"
    child = parent / "cases" / "c1"
    child.mkdir(parents=True)
    script = 'echo ok > "$1/w.txt" && ! (echo bad > "$2/w.txt") 2>/dev/null && pwd'
    res = _sb_run(sandbox, [SH, "-c", script, "sh", str(child), str(parent)], tmp_path, ro=[parent], rw=[child],
                  cwd=str(child))
    assert res.exit_code == 0, res.stderr
    assert res.stdout.strip() == str(child)
    assert (child / "w.txt").read_text() == "ok\n"
    assert not (parent / "w.txt").exists()


def test_sandbox_cannot_write_system_dirs_and_has_private_tmp(sandbox, tmp_path):
    marker_in_private_tmp = f"/tmp/leanc-sandbox-probe-{os.getpid()}"
    script = f"echo x > /usr/pwned; echo y > {marker_in_private_tmp} && cat {marker_in_private_tmp}"
    res = _sb_run(sandbox, [SH, "-c", script], tmp_path)
    assert res.stdout == "y\n"
    assert not os.path.exists("/usr/pwned")
    assert not os.path.exists(marker_in_private_tmp)


def test_sandbox_environment_is_cleared(sandbox, tmp_path, monkeypatch):
    secret = "sk-sandboxSecretValue0123456789abcd"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    res = _sb_run(sandbox, ["env"], tmp_path, env=minimal_env({"VISIBLE": "1"}))
    assert res.exit_code == 0, res.stderr
    assert secret not in res.stdout and "OPENAI_API_KEY" not in res.stdout
    assert "VISIBLE=1" in res.stdout


def test_sandbox_timeout_kills_everything_inside(sandbox, tmp_path):
    rw = tmp_path / "rw"
    rw.mkdir()
    hb = rw / "heartbeat"
    script = f'(while :; do date +%s%N > "{hb}"; sleep 0.05; done) & sleep 60'
    t0 = time.monotonic()
    res = _sb_run(sandbox, [SH, "-c", script], tmp_path, rw=[rw], limits=Limits(timeout_s=1))
    assert res.timed_out is True and time.monotonic() - t0 < 5
    time.sleep(0.3)
    first = hb.read_text()
    time.sleep(0.6)
    assert hb.read_text() == first, "a process inside the sandbox survived the timeout"


def test_sandbox_setsid_grandchild_does_not_escape(sandbox, tmp_path):
    rw = tmp_path / "rw"
    rw.mkdir()
    hb = rw / "heartbeat"
    script = f'setsid sh -c \'while :; do date +%s%N > "{hb}"; sleep 0.05; done\' & sleep 60'
    res = _sb_run(sandbox, [SH, "-c", script], tmp_path, rw=[rw], limits=Limits(timeout_s=1))
    assert res.timed_out is True
    time.sleep(0.3)
    first = hb.read_text()
    time.sleep(0.6)
    assert hb.read_text() == first


def test_sandbox_output_cap_and_cpu_limit(sandbox, tmp_path):
    res = _sb_run(sandbox, ["yes"], tmp_path, limits=Limits(timeout_s=30, max_output_bytes=8192))
    assert res.output_truncated is True and len(res.stdout) == 8192 and res.duration_s < 10
    t0 = time.monotonic()
    res = _sb_run(sandbox, [SH, "-c", "while :; do :; done"], tmp_path, limits=Limits(timeout_s=30, cpu_s=1))
    assert time.monotonic() - t0 < 10 and res.timed_out is False
    assert res.exit_code in (BWRAP_SIGNAL_EXIT_BASE + signal.SIGXCPU, BWRAP_SIGNAL_EXIT_BASE + signal.SIGKILL)


def test_sandbox_missing_executable_fails_cleanly(sandbox, tmp_path):
    res = _sb_run(sandbox, ["/nonexistent/prog"], tmp_path)
    assert res.ok is False and res.exit_code != 0
    assert "bwrap" in res.stderr


def test_network_probe_reports_loopback_only_inside_bwrap(sandbox, tmp_path):
    assert network_probe_blocked(sandbox, tmp_path) is NetworkIsolation.BLOCKED


def test_network_probe_cannot_confirm_isolation_without_a_sandbox(tmp_path):
    assert network_probe_blocked(detect_sandbox("none"), tmp_path) is NetworkIsolation.UNCONFIRMED


def _bwrap_stand_in_recording_its_environment(directory: Path) -> tuple[Sandbox, Path]:
    recorded_environ = directory / "bwrap.environ"
    stand_in = directory / "bwrap"
    stand_in.write_text(
        f"#!/bin/sh\ncat /proc/$$/environ > '{recorded_environ}'\nprintf '%s' '{LOOPBACK_ONLY_PROC_NET_DEV}'\n"
    )
    stand_in.chmod(0o755)
    return Sandbox(kind="bwrap", executable=str(stand_in)), recorded_environ


def test_network_probe_launches_bwrap_with_only_the_probe_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", PARENT_ONLY_SECRET)
    sandbox, recorded_environ = _bwrap_stand_in_recording_its_environment(tmp_path)
    assert network_probe_blocked(sandbox, tmp_path) is NetworkIsolation.BLOCKED
    assert recorded_environ.read_bytes() == f"PATH={PROBE_PATH}\0".encode()


def test_detect_sandbox_none_preference():
    sb = detect_sandbox("none")
    assert sb.kind == "none" and not sb.active and sb.note
    assert sb.wrap(["true"], cwd="/", env={}) == ["true"]


def test_bwrap_probe_matches_reality(needs_bwrap):
    sb = detect_sandbox("auto")
    argv = sb.wrap(["/usr/bin/true"], cwd="/", env={"PATH": "/usr/bin"})
    assert argv[0] == sb.executable and "--unshare-all" in argv and "--clearenv" in argv
    assert "--share-net" not in argv
    assert subprocess.run(argv, check=False, capture_output=True).returncode == 0
