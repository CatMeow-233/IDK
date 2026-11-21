"""Command output: human text or pure JSON on stdout, errors on stderr, and the per-run log file."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from leanc.foundation.models import RepairRound, RunRecord
from leanc.foundation.status import Status

LOGGER_NAME = "leanc"
FILE_ONLY_LEVEL = logging.INFO
QUIET_STDERR_LEVEL = logging.WARNING
LOG_FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
STDERR_FORMAT = "leanc: %(levelname)s: %(message)s"
SHOWN_FAILURES = 8
SHOWN_INPUTS = 8
SHOWN_STAGE_SUMMARY_CHARS = 110
SHOWN_FAILURE_DESCRIPTION_CHARS = 90
SHOWN_INPUTS_CHARS = 150
SHOWN_ROUND_NOTE_CHARS = 100


class Console:
    """Human-readable lines unless ``--json``, in which case stdout carries only the JSON document."""

    def __init__(self, as_json: bool):
        self.as_json = as_json

    def info(self, msg: str = "") -> None:
        if not self.as_json:
            print(msg)

    def err(self, msg: str) -> None:
        print(msg, file=sys.stderr)

    def emit(self, data: Any) -> None:
        if self.as_json:
            print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def setup_logging(log_file: Path | None, verbose: bool) -> None:
    """Log everything to ``log_file``; on stderr only warnings, or everything with ``verbose``."""
    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(LOG_FILE_FORMAT))
        root.addHandler(file_handler)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.DEBUG if verbose else QUIET_STDERR_LEVEL)
    stderr_handler.setFormatter(logging.Formatter(STDERR_FORMAT))
    root.addHandler(stderr_handler)


def _round_verdict(round_: RepairRound) -> str:
    if round_.policy and not round_.policy.allowed:
        return "rejected"
    return round_.status_after.value if round_.status_after else "-"


def print_run_summary(record: RunRecord, run_dir: Path, console: Console) -> None:
    vr = record.verification
    if record.provider is not None and not record.provider.real_llm:
        console.info(f"provider  {record.provider.name} (fixed fixtures; NOT a real LLM, not LLM performance)")
    if record.initial_verification is not None:
        console.info(f"initial   {record.initial_verification.status.value}")
    if vr is not None and record.status is Status.REPAIR_EXHAUSTED:
        console.info("best candidate (NOT written to the workspace):")
    if vr is not None:
        console.info(f"spec      {vr.spec_id} v{vr.spec_version}")
        for stage in vr.stages:
            console.info(f"  {stage.stage:<16} {stage.outcome.value:<13} {stage.summary[:SHOWN_STAGE_SUMMARY_CHARS]}")
        for failure in vr.all_failures()[:SHOWN_FAILURES]:
            location = failure.location.short() if failure.location else "?"
            console.info(
                f"  ! [{failure.tool}] {failure.kind}: "
                f"{failure.description[:SHOWN_FAILURE_DESCRIPTION_CHARS]} @ {location}"
            )
            if failure.inputs:
                inputs = ", ".join(f"{i.name}={i.value}" for i in failure.inputs[:SHOWN_INPUTS])
                console.info("      inputs: " + inputs[:SHOWN_INPUTS_CHARS])
    for round_ in record.repair_rounds:
        console.info(f"  repair round {round_.round}: {_round_verdict(round_)}  {round_.note[:SHOWN_ROUND_NOTE_CHARS]}")
    for message in record.messages:
        console.info(f"note: {message}")
    console.info(f"STATUS    {record.status.value}")
    console.info(f"report    {run_dir / 'report.md'}")
    console.info(f"log       {run_dir / 'leanc.log'}")
