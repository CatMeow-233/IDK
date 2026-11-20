"""Project discovery and the per-verification context handed to every backend."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from leanc.backends.tools import Toolchain, toolchain
from leanc.core.hashing import sha256_bytes
from leanc.core.runner import Limits, ProcResult, minimal_env, run_command
from leanc.core.sandbox import Sandbox, SandboxMounts, detect_sandbox
from leanc.core.workspace import Workspace
from leanc.specs.model import Spec

PROJECT_MARKER = "leanc.yaml"


def find_project_root(spec_path: Path) -> Path:
    """Nearest ancestor of the spec containing ``leanc.yaml``; otherwise the
    spec's own directory.  All paths inside a spec are relative to this root."""
    start = spec_path.resolve().parent
    for d in (start, *start.parents):
        if (d / PROJECT_MARKER).is_file():
            return d
    return start


def leanc_include_dir() -> Path:
    """Directory holding ``leanc.h`` (package data)."""
    return Path(str(resources.files("leanc") / "data" / "include"))


def new_run_id(prefix: str = "") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}{ts}-{secrets.token_hex(3)}"


def runs_dir(root: Path) -> Path:
    return Path(root) / ".leanc" / "runs"


@dataclass
class VerifyOptions:
    allow_unsandboxed: bool = False  # permit dynamic execution without a sandbox (explicit opt-in)
    sandbox_preference: str = "auto"  # "auto" | "none"
    timeout_scale: float = 1.0  # multiply every tool timeout (CLI --timeout-scale)
    gates: list[str] | None = None  # restrict to a subset (cannot add gates the spec lacks)
    fail_fast: bool = False
    # Run the gates even if the spec is not approved (informational only: the
    # overall status stays SPEC_UNAPPROVED).
    check_unapproved: bool = False


@dataclass
class BackendContext:
    """Everything a backend needs.  Backends must:

    * read inputs only from ``snapshot_dir`` (a verified copy of the project
      files, laid out with the same relative paths as the workspace),
    * write outputs only under ``build_dir`` / ``raw_dir`` / ``run_dir/logs``,
    * execute tools via :meth:`run` (sandboxed, bounded, no shell).
    """

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

    # ---------------------------------------------------------------- helpers
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
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in what)[:60]
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
        """Run a tool.  ``sandboxed=True`` uses the sandbox when one is
        available; callers that execute *untrusted programs* must additionally
        check :meth:`may_execute_untrusted` first.  ``mounts`` replaces the
        default bind set (:meth:`mounts`) so untrusted programs can be given
        less than the tools (e.g. a single writable per-test directory)."""
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
    """Create run directories and snapshot the project files the spec names.

    The snapshot is what the tools see; hashes of the snapshot are what get
    recorded (and what the approval check compares against), which closes the
    time-of-check / time-of-use gap between checking and verifying.
    """
    options = options or VerifyOptions()
    ws = Workspace(project_root)
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot = run_dir / "snapshot"
    build = run_dir / "build"
    raw = run_dir / "raw"
    for d in (snapshot, build, raw, run_dir / "logs"):
        d.mkdir(parents=True, exist_ok=True)
    files = list(dict.fromkeys([*spec.sources.editable, *spec.protected_paths()]))
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
