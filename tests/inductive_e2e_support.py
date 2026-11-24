from __future__ import annotations

import json
from pathlib import Path

from leanc.specs.model import INDUCTIVE_GATE

VERIFICATION_REPORT = Path("verify") / "verification.md"
VERIFICATION_RECORD = Path("verify") / "verification.json"
CLAIM_MARKERS = ("claimed functions:", "contracted loops proved by induction")
NO_CLAIM_FORMAT = "the inductive gate ended {outcome}: it establishes no inductive claim"


def stages_by_name(verification: dict) -> dict[str, dict]:
    return {stage["stage"]: stage for stage in verification["stages"]}


def report_texts(payload: dict) -> dict[str, str]:
    run_report = Path(payload["report"])
    return {
        "run report": run_report.read_text(encoding="utf-8"),
        "verification report": (Path(payload["run_dir"]) / VERIFICATION_REPORT).read_text(encoding="utf-8"),
    }


def inductive_record(payload: dict) -> dict:
    record = json.loads((Path(payload["run_dir"]) / VERIFICATION_RECORD).read_text(encoding="utf-8"))
    return next(stage for stage in record["stages"] if stage["stage"] == INDUCTIVE_GATE)


def flat(text: str) -> str:
    return " ".join(text.replace("\\*", "*").replace("\\_", "_").split())


def reason_pairs(verification: dict) -> set[tuple[str, str]]:
    return {(code["code"], code["stage"]) for code in verification["reason_codes"]}


def mentions_claim(text: str) -> bool:
    return any(marker in text for marker in CLAIM_MARKERS)


def claims_anything(verification: dict) -> bool:
    return any(mentions_claim(assumption) for assumption in verification["assumptions"])


def no_claim_statement(outcome: str) -> str:
    return NO_CLAIM_FORMAT.format(outcome=outcome)
