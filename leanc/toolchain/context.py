from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Literal

from leanc.foundation.hashing import sha256_bytes
from leanc.foundation.runner import Limits, ProcResult, minimal_env, run_command
from leanc.foundation.sandbox import Sandbox, SandboxMounts, detect_sandbox
from leanc.foundation.text import safe_name
from leanc.foundation.workspace import Workspace
from leanc.specs.model import Spec
from leanc.toolchain.environment import Toolchain, toolchain

PROJECT_MARKER = "leanc.yaml"
LOG_NAME_MAX_LENGTH = 60

SandboxPreference = Literal["auto", "none"]


def find_project_root(spec_path: Path) -> Path:
    start = spec_path.resolve().parent
    for d in (start, *start.parents):
        if (d / PROJECT_MARKER).is_file():
            return d
    return start


def leanc_include_dir() -> Path:
    return Path(str(resources.files("leanc") / "data" / "include"))


def leanc_runtime_dir() -> Path:
    return Path(str(resources.files("leanc") / "data" / "runtime"))


def new_run_id(prefix: str = "") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}{ts}-{secrets.token_hex(3)}"


def runs_dir(root: Path) -> Path:
    return Path(root) / ".leanc" / "runs"


@dataclass
class VerifyOptions:
    allow_unsandboxed: bool = False
    sandbox_preference: SandboxPreference = "auto"
    timeout_scale: float = 1.0
    gates: list[str] | None = None
    fail_fast: bool = False
    check_unapproved: bool = False


@dataclass
class BackendContext:
    spec: Spec
    project_root: Path
    ws: Workspace
    run_dir: Path
    snapshot_dir: Path
    build_dir: Path
    raw_dir: Path
    sandbox: Sandbox
    tools: Toolchain
    options: VerifyOptions = field(default_factory=VerifyOptions)
    _counter: int = 0

    @property
    def include_dir(self) -> Path:
        return leanc_include_dir()

    def include_args(self) -> list[str]:
        args = []
        for d in self.spec.sources.include_dirs:
            args.append(f"-I{d}")
        return args

    def timeout(self, seconds: float) -> float:
        return max(1.0, float(seconds) * float(self.options.timeout_scale))

    def log_name(self, stage: str, what: str) -> str:
        self._counter += 1
        safe = safe_name(what, LOG_NAME_MAX_LENGTH)
        return f"{self._counter:02d}-{stage}-{safe}"

    def mounts(self, extra_ro: list[str] | None = None) -> SandboxMounts:
        ro = [str(self.snapshot_dir), str(self.include_dir)] + list(extra_ro or [])
        rw = [str(self.build_dir), str(self.raw_dir)]
        return SandboxMounts(ro=ro, rw=rw)

    def run(
        self,
        argv: list[str],
        *,
        limits: Limits,
        cwd: Path | None = None,
        env_extra: dict[str, str] | None = None,
        stdin_data: bytes | None = None,
        sandboxed: bool = True,
        mounts: SandboxMounts | None = None,
    ) -> ProcResult:
        env = minimal_env(env_extra, home="/tmp")
        return run_command(
            argv,
            cwd=cwd or self.snapshot_dir,
            limits=limits,
            env=env,
            stdin_data=stdin_data,
            sandbox=self.sandbox if (sandboxed and self.sandbox.active) else None,
            mounts=mounts if mounts is not None else self.mounts(),
        )

    def may_execute_untrusted(self) -> tuple[bool, str]:
        if self.sandbox.active:
            return True, f"sandbox: {self.sandbox.kind}"
        if self.options.allow_unsandboxed:
            return True, "WARNING: executing untrusted code WITHOUT a sandbox (explicit --allow-unsandboxed)"
        return False, (
            "no functional sandbox available, refusing to execute untrusted programs: "
            + (self.sandbox.note or "")
            + " (pass --allow-unsandboxed to override at your own risk)"
        )

    def relrun(self, p: Path) -> str:
        return os.path.relpath(p, self.run_dir)


def make_context(
    spec: Spec,
    project_root: Path,
    run_dir: Path,
    options: VerifyOptions | None = None,
) -> BackendContext:
    options = options or VerifyOptions()
    ws = Workspace(project_root)
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot = run_dir / "snapshot"
    build = run_dir / "build"
    raw = run_dir / "raw"
    for d in (snapshot, build, raw, run_dir / "logs"):
        d.mkdir(parents=True, exist_ok=True)
    files = list(dict.fromkeys([*spec.editable_paths(), *spec.protected_paths()]))
    existing = [f for f in files if ws.exists(f)]
    ws.copy_into(existing, snapshot)
    return BackendContext(
        spec=spec,
        project_root=Path(project_root),
        ws=ws,
        run_dir=run_dir,
        snapshot_dir=snapshot,
        build_dir=build,
        raw_dir=raw,
        sandbox=detect_sandbox(options.sandbox_preference),
        tools=toolchain(spec.compile.compiler),
        options=options,
    )


def snapshot_hashes(ctx: BackendContext) -> dict[str, str]:
    out = {}
    for p in sorted(ctx.snapshot_dir.rglob("*")):
        if p.is_file():
            out[p.relative_to(ctx.snapshot_dir).as_posix()] = sha256_bytes(p.read_bytes())
    return out
