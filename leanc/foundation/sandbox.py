from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Literal, Mapping, Sequence

SandboxKind = Literal["bwrap", "none"]

SYSTEM_RO_DIRS = ("/usr", "/etc/alternatives")
SYSTEM_RO_FILES = ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/localtime")
MERGED_USR_LINKS = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
TMPFS_SIZE_MB = 256
TMPFS_SIZES_MOST_RESTRICTIVE_FIRST: tuple[int | None, ...] = (TMPFS_SIZE_MB, None)
MIB = 1024 * 1024
NOBODY_ID = 65534
PROBE_TIMEOUT_S = 20
PROBE_PATH = "/usr/bin:/bin"
LOOPBACK_INTERFACE = "lo"
PROC_NET_DEV_HEADER_LINES = 2

BWRAP_MISSING_NOTE = (
    "bubblewrap (bwrap) not found; install it (e.g. `apt-get install bubblewrap`) "
    "to enable isolated execution of untrusted programs"
)
NON_LINUX_SANDBOX_NOTE = (
    "bubblewrap (bwrap) is Linux-only and unavailable on {platform}; untrusted execution gates "
    "(sanitizer, lean) are skipped unless --allow-unsandboxed is passed or run inside a Linux container"
)


@dataclass
class SandboxMounts:
    ro: list[str] = field(default_factory=list)
    rw: list[str] = field(default_factory=list)


@dataclass
class Sandbox:
    kind: SandboxKind
    executable: str | None = None
    note: str = ""
    allow_network: bool = False
    tmpfs_size_mb: int | None = None

    @property
    def active(self) -> bool:
        return self.kind != "none"

    def wrap(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        mounts: SandboxMounts | None = None,
    ) -> list[str]:
        if self.kind == "none":
            return list(argv)
        if self.kind != "bwrap":
            raise ValueError(f"unsupported sandbox kind {self.kind!r}")
        assert self.executable
        args: list[str] = [
            self.executable,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
        ]
        if self.allow_network:
            args.append("--share-net")
        args += ["--cap-drop", "ALL", "--uid", str(NOBODY_ID), "--gid", str(NOBODY_ID)]
        args += _system_mount_args()
        args += ["--proc", "/proc", "--dev", "/dev"]
        args += self._cap_tmpfs_for_untrusted_code()
        args += ["--tmpfs", "/tmp"]
        args += _bind_args(_program_dir_outside_system_dirs(argv[0], env), mounts or SandboxMounts())
        args += ["--chdir", cwd, "--clearenv"]
        for name, value in env.items():
            args += ["--setenv", name, value]
        args.append("--")
        args.extend(argv)
        return args

    def _cap_tmpfs_for_untrusted_code(self) -> list[str]:
        if not self.tmpfs_size_mb:
            return []
        return ["--size", str(int(self.tmpfs_size_mb) * MIB)]


def _covered_by_system(path: str) -> bool:
    return any(path == d or path.startswith(d + "/") for d in SYSTEM_RO_DIRS)


def _program_dir_outside_system_dirs(program: str, env: Mapping[str, str]) -> str | None:
    executable = program if os.path.isabs(program) else shutil.which(program, path=env.get("PATH"))
    if not executable:
        return None
    real_dir = os.path.dirname(os.path.realpath(executable))
    return None if _covered_by_system(real_dir) else real_dir


def _bind_args(program_dir: str | None, mounts: SandboxMounts) -> list[str]:
    args: list[str] = []
    bound: set[str] = set()
    if program_dir is not None:
        args += ["--ro-bind", program_dir, program_dir]
        bound.add(program_dir)
    for path in mounts.ro:
        real = os.path.realpath(path)
        if real in bound:
            continue
        args += ["--ro-bind", real, real]
        bound.add(real)
    for path in mounts.rw:
        real = os.path.realpath(path)
        args += ["--bind", real, real]
        bound.add(real)
    return args


@lru_cache(maxsize=1)
def _system_mount_args_cached() -> tuple[str, ...]:
    args: list[str] = []
    for d in SYSTEM_RO_DIRS:
        if os.path.isdir(d):
            args += ["--ro-bind", d, d]
    for link in MERGED_USR_LINKS:
        if os.path.islink(link):
            args += ["--symlink", os.readlink(link), link]
        elif os.path.isdir(link):
            args += ["--ro-bind", link, link]
    for f in SYSTEM_RO_FILES:
        if os.path.exists(f):
            args += ["--ro-bind", f, f]
    return tuple(args)


def _system_mount_args() -> list[str]:
    return list(_system_mount_args_cached())


def _true_in_bwrap_argv(executable: str, true_bin: str, tmpfs_size_mb: int | None) -> list[str]:
    probe = Sandbox(kind="bwrap", executable=executable, tmpfs_size_mb=tmpfs_size_mb)
    return probe.wrap([true_bin], cwd="/", env={"PATH": PROBE_PATH})


def _functional_bwrap(executable: str, tmpfs_size_mb: int | None) -> Sandbox:
    return Sandbox(
        kind="bwrap",
        executable=executable,
        tmpfs_size_mb=tmpfs_size_mb,
        note="bubblewrap: unshared user/pid/net/ipc/uts/cgroup namespaces, no network, "
        "caps dropped, uid nobody, read-only system dirs, private /tmp"
        + (f" (capped at {tmpfs_size_mb} MB)" if tmpfs_size_mb else ""),
    )


def _broken_bwrap(failed_probe: subprocess.CompletedProcess[bytes]) -> Sandbox:
    err = failed_probe.stderr.decode("utf-8", "replace").strip().splitlines()
    return Sandbox(
        kind="none",
        note="bwrap present but not functional (user namespaces unavailable?): "
        + (err[-1] if err else f"exit {failed_probe.returncode}"),
    )


@lru_cache(maxsize=4)
def detect_sandbox(preference: str = "auto") -> Sandbox:
    if preference == "none":
        return Sandbox(kind="none", note="sandbox disabled by configuration")
    if not sys.platform.startswith("linux"):
        return Sandbox(kind="none", note=NON_LINUX_SANDBOX_NOTE.format(platform=sys.platform))
    executable = shutil.which("bwrap")
    if not executable:
        return Sandbox(kind="none", note=BWRAP_MISSING_NOTE)
    true_bin = shutil.which("true") or "/usr/bin/true"
    for tmpfs_size_mb in TMPFS_SIZES_MOST_RESTRICTIVE_FIRST:
        argv = _true_in_bwrap_argv(executable, true_bin, tmpfs_size_mb)
        try:
            probe = subprocess.run(
                argv, capture_output=True, timeout=PROBE_TIMEOUT_S, check=False, env={"PATH": PROBE_PATH}
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Sandbox(kind="none", note=f"bwrap present but not functional: {exc}")
        if probe.returncode == 0:
            return _functional_bwrap(executable, tmpfs_size_mb)
    return _broken_bwrap(probe)


class NetworkIsolation(Enum):
    BLOCKED = "blocked"
    VISIBLE = "VISIBLE"
    UNCONFIRMED = "COULD NOT CONFIRM isolation"


def _interface_names(proc_net_dev: bytes) -> list[str]:
    lines = proc_net_dev.decode("utf-8", "replace").splitlines()[PROC_NET_DEV_HEADER_LINES:]
    return [line.split(":", 1)[0].strip() for line in lines if ":" in line]


def network_probe_blocked(sandbox: Sandbox, work_dir: Path) -> NetworkIsolation:
    if not sandbox.active:
        return NetworkIsolation.UNCONFIRMED
    cat = shutil.which("cat") or "/usr/bin/cat"
    argv = sandbox.wrap([cat, "/proc/net/dev"], cwd="/", env={"PATH": PROBE_PATH})
    try:
        probe = subprocess.run(
            argv, capture_output=True, timeout=PROBE_TIMEOUT_S, check=False, env={"PATH": PROBE_PATH}
        )
    except (OSError, subprocess.TimeoutExpired):
        return NetworkIsolation.UNCONFIRMED
    if probe.returncode != 0:
        return NetworkIsolation.UNCONFIRMED
    if all(name == LOOPBACK_INTERFACE for name in _interface_names(probe.stdout)):
        return NetworkIsolation.BLOCKED
    return NetworkIsolation.VISIBLE
