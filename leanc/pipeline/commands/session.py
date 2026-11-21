from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from leanc import __version__
from leanc.foundation.errors import LeanCError
from leanc.foundation.models import RepairRound, RunRecord, utcnow
from leanc.foundation.status import EXIT_USAGE, Status, exit_code
from leanc.llm.base import LLMProvider
from leanc.llm.factory import get_provider
from leanc.pipeline.commands.console import LOGGER_NAME, Console, print_run_summary
from leanc.pipeline.config import load_config
from leanc.pipeline.controller import ensure_runs_dir
from leanc.reports.json_report import atomic_write_text, summary, write_json
from leanc.reports.markdown import render_run_md
from leanc.toolchain.context import VerifyOptions, find_project_root, new_run_id

log = logging.getLogger(LOGGER_NAME)

GENERATED_UNVERIFIED_STATUS = Status.UNKNOWN
GENERATION_SUCCEEDED_EXIT_CODE = 0
DEFAULT_TIMEOUT_SCALE = 1.0
LLM_TIMEOUT_ENV = "LEANC_LLM_TIMEOUT"
IGNORED_CONFIG_UNSANDBOXED_WARNING = (
    "leanc: warning: 'allow_unsandboxed' in leanc.yaml is ignored (a project file cannot disable "
    "isolation); pass --allow-unsandboxed explicitly if you accept the risk"
)
UNSANDBOXED_WARNING = "leanc: WARNING: --allow-unsandboxed: untrusted test programs may run WITHOUT isolation"


def is_unverified_generation(record: RunRecord) -> bool:
    return record.command == "generate" and record.status is GENERATED_UNVERIFIED_STATUS


def explicit_root(args) -> Path | None:
    given = getattr(args, "project_root", None)
    if not given:
        return None
    root = Path(given).resolve()
    if not root.is_dir():
        raise LeanCError(f"--project-root {given} is not an existing directory")
    return root


def spec_and_root(args) -> tuple[Path, Path]:
    spec_path = Path(args.spec)
    if spec_path.is_symlink():
        raise LeanCError(f"{spec_path}: spec must not be a symbolic link")
    if not spec_path.is_file():
        raise LeanCError(f"spec file not found: {spec_path}")
    root = explicit_root(args) or find_project_root(spec_path)
    return spec_path.resolve(), root


def _selected_gates(args) -> list[str] | None:
    if not getattr(args, "gates", None):
        return None
    return [gate.strip() for gate in args.gates.split(",") if gate.strip()]


def verify_options(args, root: Path) -> VerifyOptions:
    config = load_config(root)
    gates = _selected_gates(args)
    sandbox_preference = "none" if getattr(args, "no_sandbox", False) else config.sandbox
    if config.allow_unsandboxed:
        print(IGNORED_CONFIG_UNSANDBOXED_WARNING, file=sys.stderr)
    if getattr(args, "allow_unsandboxed", False):
        print(UNSANDBOXED_WARNING, file=sys.stderr)
    return VerifyOptions(
        allow_unsandboxed=bool(getattr(args, "allow_unsandboxed", False)),
        sandbox_preference=sandbox_preference,
        timeout_scale=float(getattr(args, "timeout_scale", DEFAULT_TIMEOUT_SCALE) or DEFAULT_TIMEOUT_SCALE),
        gates=gates,
        fail_fast=bool(getattr(args, "fail_fast", False)),
        check_unapproved=bool(getattr(args, "check_unapproved", False)),
    )


def make_provider(args, root: Path, run_dir: Path) -> LLMProvider:
    config = load_config(root)
    name = getattr(args, "provider", None) or config.provider
    if getattr(args, "llm_timeout", None):
        os.environ[LLM_TIMEOUT_ENV] = str(args.llm_timeout)
    mock_dir = Path(args.mock_dir).resolve() if getattr(args, "mock_dir", None) else root / config.mock_dir
    return get_provider(name, project_root=root, mock_dir=mock_dir, transcript_dir=run_dir / "llm")


@dataclass(frozen=True)
class ActiveRun:
    run_id: str
    run_dir: Path
    root: Path
    command: str


@dataclass
class _ActiveRunSlot:
    run: ActiveRun | None = None


_ACTIVE = _ActiveRunSlot()


def new_run(root: Path, prefix: str) -> tuple[str, Path]:
    run_id = new_run_id(prefix)
    run_dir = ensure_runs_dir(root) / run_id
    run_dir.mkdir(parents=False, exist_ok=False)
    _ACTIVE.run = ActiveRun(run_id=run_id, run_dir=run_dir, root=root, command=prefix.rstrip("-"))
    return run_id, run_dir


def record_aborted_run(message: str) -> None:
    run = _ACTIVE.run
    if run is None or (run.run_dir / "run.json").exists():
        return
    record = RunRecord(
        run_id=run.run_id,
        command=run.command,
        leanc_version=__version__,
        status=Status.TOOL_ERROR,
        project_root=str(run.root),
        messages=[f"aborted before completion (input error, exit {EXIT_USAGE}): {message}"],
        finished_at=utcnow(),
    )
    write_json(record, run.run_dir / "run.json")
    atomic_write_text(run.run_dir / "report.md", render_run_md(record))


def _round_summary(round_: RepairRound) -> dict:
    return {
        "round": round_.round,
        "status_after": round_.status_after.value if round_.status_after else None,
        "policy_allowed": round_.policy.allowed if round_.policy else None,
        "violations": [v.rule for v in (round_.policy.violations if round_.policy else [])],
        "reverted": round_.reverted,
        "note": round_.note,
    }


def _payload(record: RunRecord, run_dir: Path, exit_status: int) -> dict:
    payload = {
        "run_id": record.run_id,
        "command": record.command,
        "status": record.status.value,
        "exit_code": exit_status,
        "provider": record.provider.model_dump() if record.provider else None,
        "run_dir": str(run_dir),
        "report": str(run_dir / "report.md"),
        "messages": record.messages,
    }
    if record.initial_verification is not None:
        payload["initial_status"] = record.initial_verification.status.value
    if record.verification is not None:
        payload["verification"] = summary(record.verification)
    if record.repair_rounds:
        payload["repair_rounds"] = [_round_summary(r) for r in record.repair_rounds]
    return payload


def finish_run(record: RunRecord, run_dir: Path, console: Console, *, exit_override: int | None = None) -> int:
    record.finished_at = utcnow()
    record.artifacts.setdefault("run_json", "run.json")
    record.artifacts.setdefault("report_md", "report.md")
    record.artifacts.setdefault("log", "leanc.log")
    write_json(record, run_dir / "run.json")
    atomic_write_text(run_dir / "report.md", render_run_md(record))
    exit_status = exit_override if exit_override is not None else exit_code(record.status)
    console.emit(_payload(record, run_dir, exit_status))
    if not console.as_json:
        print_run_summary(record, run_dir, console)
    for message in record.messages:
        log.info("%s", message)
    log.info("final status %s", record.status.value)
    return exit_status
