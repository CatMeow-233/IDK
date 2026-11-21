from __future__ import annotations

import json
import sys
from pathlib import Path

from leanc.foundation.errors import LeanCError
from leanc.foundation.hashing import sha256_bytes
from leanc.foundation.models import RunRecord
from leanc.foundation.status import Status, exit_code, is_success
from leanc.foundation.workspace import Workspace
from leanc.pipeline.commands.session import GENERATION_SUCCEEDED_EXIT_CODE, explicit_root, is_unverified_generation
from leanc.pipeline.config import CONFIG_FILE
from leanc.reports.json_report import read_run_record, summary
from leanc.reports.markdown import render_run_md
from leanc.specs.loader import load_spec, spec_hash
from leanc.toolchain.context import runs_dir

LATEST_RUN = "latest"
STALE_SUCCESS_STATUS = Status.UNKNOWN
STALE_WARNING = (
    "leanc: warning: STALE: the current workspace differs from what this run verified, so its status does "
    "not describe the current workspace: "
)


def discover_root(start: Path) -> Path:
    start = start.resolve()
    for directory in (start, *start.parents):
        if (directory / CONFIG_FILE).is_file() or (directory / ".leanc").is_dir():
            return directory
    return start


def find_run(arg: str | None, root: Path) -> Path:
    if arg and Path(arg).exists():
        given = Path(arg)
        return given.parent if given.is_file() else given
    all_runs = runs_dir(root)
    if not all_runs.is_dir():
        raise LeanCError(f"no runs found under {all_runs}")
    if not arg or arg == LATEST_RUN:
        recorded = [d for d in all_runs.iterdir() if d.is_dir() and (d / "run.json").exists()]
        if not recorded:
            raise LeanCError(f"no runs found under {all_runs}")
        return max(recorded, key=lambda d: (d / "run.json").stat().st_mtime)
    named = all_runs / arg
    if not named.is_dir():
        raise LeanCError(f"run {arg!r} not found under {all_runs}")
    return named


def _stale_spec(spec_path: str, verified_hash: str) -> list[str]:
    try:
        current = load_spec(spec_path)
    except LeanCError:
        return [f"{spec_path} (spec unreadable)"]
    return [f"{spec_path} (spec content)"] if spec_hash(current) != verified_hash else []


def _is_stale_source(ws: Workspace, rel: str, verified_hash: str) -> bool:
    try:
        content = ws.read_bytes(rel)
    except LeanCError:
        return True
    return sha256_bytes(content) != verified_hash


def stale_files(record: RunRecord) -> list[str]:
    vr = record.verification
    if vr is None or not vr.source_hashes or not record.project_root:
        return []
    try:
        ws = Workspace(record.project_root)
    except LeanCError:
        return sorted(vr.source_hashes)
    stale = _stale_spec(record.spec_path, vr.spec_hash) if record.spec_path and vr.spec_hash else []
    stale.extend(rel for rel, digest in vr.source_hashes.items() if _is_stale_source(ws, rel, digest))
    return stale


def _report_exit_code(record: RunRecord, stale: list[str]) -> int:
    if stale and is_success(record.status):
        return exit_code(STALE_SUCCESS_STATUS)
    if is_unverified_generation(record):
        return GENERATION_SUCCEEDED_EXIT_CODE
    return exit_code(record.status)


def cmd_report(args) -> int:
    root = explicit_root(args) or discover_root(Path("."))
    run = find_run(args.run, root)
    record = read_run_record(run / "run.json")
    stale = stale_files(record)
    if args.format == "json":
        data = record.model_dump(mode="json")
        if record.verification:
            data["summary"] = summary(record.verification)
        data["stale_files"] = stale
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(render_run_md(record))
    if stale:
        print(STALE_WARNING + ", ".join(stale), file=sys.stderr)
    return _report_exit_code(record, stale)
