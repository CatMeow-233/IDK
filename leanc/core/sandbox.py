"""Sandbox detection and argv wrapping.

Lean C treats generated code as untrusted input.  Dynamic execution of
untrusted programs (sanitizer test runs) requires an isolation mechanism.  V1
uses `bubblewrap <https://github.com/containers/bubblewrap>`_ (``bwrap``):

* new user / pid / net / ipc / uts / cgroup namespaces (``--unshare-all``):
  **no network access**;
* all capabilities dropped, uid/gid mapped to ``nobody``;
* read-only system directories, a private ``/tmp`` tmpfs, ``/proc`` and a
  minimal ``/dev``; only explicitly listed directories are bound;
* cleared environment;
* the sandbox dies with its parent.

If ``bwrap`` is missing or not functional (e.g. inside an unprivileged
container without user namespaces), :func:`detect_sandbox` returns a
``kind="none"`` sandbox with an explanation.  Callers must then refuse dynamic
execution unless the user explicitly opted in to unsandboxed execution; Lean C
never pretends a sandbox is active.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Sequence

SYSTEM_RO_DIRS = ("/usr", "/etc/alternatives")
SYSTEM_RO_FILES = ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/localtime")
MERGED_USR_LINKS = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
TMPFS_SIZE_MB = 256


@dataclass
class SandboxMounts:
    """Extra bind mounts.  Paths are bound at the same location inside."""

    ro: list[str] = field(default_factory=list)
    rw: list[str] = field(default_factory=list)


@dataclass
class Sandbox:
    kind: str  # "bwrap" | "none"
    executable: str | None = None
    note: str = ""
    allow_network: bool = False
    tmpfs_size_mb: int | None = None  # set by detect_sandbox when bwrap supports --size

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
        if self.kind != "bwrap":  # pragma: no cover - defensive
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
        args += ["--cap-drop", "ALL", "--uid", "65534", "--gid", "65534"]
        args += _system_mount_args()
        args += ["--proc", "/proc", "--dev", "/dev"]
        if self.tmpfs_size_mb:
            # size-capped private /tmp (bubblewrap >= 0.8): untrusted code
            # cannot exhaust host memory through tmpfs writes
            args += ["--size", str(int(self.tmpfs_size_mb) * 1024 * 1024)]
        args += ["--tmpfs", "/tmp"]
        mounts = mounts or SandboxMounts()
        bound: set[str] = set()
        # Make sure the executable itself is visible.
        exe = shutil.which(argv[0], path=env.get("PATH")) if not os.path.isabs(argv[0]) else argv[0]
        if exe:
            real_dir = os.path.dirname(os.path.realpath(exe))
            if not _covered_by_system(real_dir):
                args += ["--ro-bind", real_dir, real_dir]
                bound.add(real_dir)
        for p in mounts.ro:
            rp = os.path.realpath(p)
            if rp in bound:
                continue
            args += ["--ro-bind", rp, rp]
            bound.add(rp)
        for p in mounts.rw:
            rp = os.path.realpath(p)
            args += ["--bind", rp, rp]
            bound.add(rp)
        args += ["--chdir", cwd, "--clearenv"]
        for k, v in env.items():
            args += ["--setenv", k, v]
        args.append("--")
        args.extend(argv)
        return args


def _covered_by_system(path: str) -> bool:
    for d in SYSTEM_RO_DIRS:
        if path == d or path.startswith(d + "/"):
            return True
    return False


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


@lru_cache(maxsize=4)
def detect_sandbox(preference: str = "auto") -> Sandbox:
    """Return the best available sandbox.

    ``preference``: ``"auto"`` (bwrap if functional) or ``"none"``.
    """
    if preference == "none":
        return Sandbox(kind="none", note="sandbox disabled by configuration")
    exe = shutil.which("bwrap")
    if not exe:
        return Sandbox(
            kind="none",
            note="bubblewrap (bwrap) not found; install it (e.g. `apt-get install bubblewrap`) "
            "to enable isolated execution of untrusted programs",
        )
    true_bin = shutil.which("true") or "/usr/bin/true"
    size_mb: int | None = TMPFS_SIZE_MB
    for attempt in (TMPFS_SIZE_MB, None):  # prefer a size-capped /tmp
        probe = Sandbox(kind="bwrap", executable=exe, tmpfs_size_mb=attempt)
        argv = probe.wrap([true_bin], cwd="/", env={"PATH": "/usr/bin:/bin"})
        try:
            cp = subprocess.run(  # noqa: S603 - fixed argv
                argv, capture_output=True, timeout=20, check=False, env={"PATH": "/usr/bin:/bin"}
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Sandbox(kind="none", note=f"bwrap present but not functional: {exc}")
        size_mb = attempt
        if cp.returncode == 0:
            break
    if cp.returncode != 0:
        err = cp.stderr.decode("utf-8", "replace").strip().splitlines()
        return Sandbox(
            kind="none",
            note="bwrap present but not functional (user namespaces unavailable?): "
            + (err[-1] if err else f"exit {cp.returncode}"),
        )
    return Sandbox(
        kind="bwrap",
        executable=exe,
        tmpfs_size_mb=size_mb,
        note="bubblewrap: unshared user/pid/net/ipc/uts/cgroup namespaces, no network, "
        "caps dropped, uid nobody, read-only system dirs, private /tmp"
        + (f" (capped at {size_mb} MB)" if size_mb else ""),
    )


def network_probe_blocked(sandbox: Sandbox, work_dir: Path) -> bool | None:  # pragma: no cover - diagnostic
    """Best-effort check used by ``leanc doctor``: returns True when a process in
    the sandbox cannot see any non-loopback network interface."""
    if not sandbox.active:
        return None
    cat = shutil.which("cat") or "/usr/bin/cat"
    argv = sandbox.wrap([cat, "/proc/net/dev"], cwd="/", env={"PATH": "/usr/bin:/bin"})
    try:
        cp = subprocess.run(argv, capture_output=True, timeout=20, check=False)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    ifaces = [
        line.split(":", 1)[0].strip()
        for line in cp.stdout.decode("utf-8", "replace").splitlines()[2:]
        if ":" in line
    ]
    return all(i == "lo" for i in ifaces)
