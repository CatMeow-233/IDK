from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from inductive_build_support import BOUNDED_HARNESS, CONTRACTS, INDUCTIVE_HARNESS, PIPELINE_FIXTURES
from inductive_replay_support import RECORDINGS, Recorder, Replayer, load_recording, to_placeholders

from leanc.backends.inductive import run_inductive
from leanc.foundation.models import StageResult
from leanc.foundation.reasons import Reason
from leanc.pipeline.reason_codes import stage_reason_codes
from leanc.specs.model import Spec
from leanc.toolchain.context import BackendContext, VerifyOptions, make_context

TALLY_CBMC_RECORDING = RECORDINGS / "tally_cbmc.json"
VERIFY_LABEL = "verify cbmc"
VACUITY_LABEL = "vacuity cbmc"
CBMC_LABELS = (VERIFY_LABEL, VACUITY_LABEL)
KEPT_RESULT_FIELDS = ("property", "description", "status")
KEPT_LOCATION_FIELDS = ("file", "line", "function")
VERSION_PREFIX = "CBMC version"
KEPT_MESSAGE_TYPES = ("ERROR", "WARNING")
RETURN_VALUE_PREFIX = "return_value_"
ASSIGNMENT_STEP = "assignment"
CONTRACTS_LIBRARY_FILE = "<builtin-library-__CPROVER_contracts_library>"
HOLDS = "SUCCESS"

ISET_ACCEPTANCE = PIPELINE_FIXTURES.parent / "acceptance" / "iset"
ISET_PIPELINE = PIPELINE_FIXTURES / "iset"
ISET_SOURCE = "src/iset.c"
ISET_HEADER = "include/iset.h"
ISET_FUNCTIONS = ("iset_rank", "iset_contains")
ISET_SIGNATURE = "int iset_rank(const struct iset *s, int x)"

Results = list[dict[str, Any]]


def iset_spec_data(functions: tuple[str, ...] = ISET_FUNCTIONS) -> dict[str, Any]:
    return {
        "spec_id": "iset-inductive",
        "version": 1,
        "title": "iset inductive gate",
        "requirement": "test",
        "target": {"function": functions[0], "signature": ISET_SIGNATURE, "returns": {"type": "int"}},
        "sources": {"editable": [ISET_SOURCE], "headers": [ISET_HEADER], "include_dirs": ["include"]},
        "verification": {"harness": BOUNDED_HARNESS, "unwind": 2, "timeout_s": 300},
        "inductive": {"harness": INDUCTIVE_HARNESS, "contracts": CONTRACTS, "functions": list(functions)},
        "gates": ["compile", "cbmc"],
    }


def iset_context(tmp_path: Path, variant: str) -> BackendContext:
    root = tmp_path / "proj"
    for rel in (ISET_SOURCE, ISET_HEADER):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ISET_ACCEPTANCE / variant / rel, root / rel)
    for rel, source in ((INDUCTIVE_HARNESS, "harness/inductive.c"), (CONTRACTS, "loops.json")):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ISET_PIPELINE / source, root / rel)
    (root / BOUNDED_HARNESS).write_text("int main(void) { return 0; }\n", encoding="utf-8")
    spec = Spec.model_validate(iset_spec_data())
    return make_context(spec, root, tmp_path / "run", VerifyOptions(sandbox_preference="none"))


def _kept_step(step: Any) -> bool:
    return isinstance(step, dict) and step.get("stepType") == ASSIGNMENT_STEP and str(step.get("lhs", "")).startswith(RETURN_VALUE_PREFIX)


def _compact_result(result: dict[str, Any]) -> dict[str, Any]:
    compact = {key: result[key] for key in KEPT_RESULT_FIELDS if key in result}
    location = result.get("sourceLocation")
    if isinstance(location, dict):
        compact["sourceLocation"] = {key: location[key] for key in KEPT_LOCATION_FIELDS if key in location}
    steps = [{"stepType": ASSIGNMENT_STEP, "lhs": step["lhs"]} for step in result.get("trace", []) if _kept_step(step)]
    if steps:
        compact["trace"] = steps
    return compact


def _recorded(result: dict[str, Any]) -> bool:
    location = result.get("sourceLocation")
    in_library = isinstance(location, dict) and location.get("file") == CONTRACTS_LIBRARY_FILE
    return not (in_library and result.get("status") == HOLDS)


def _kept_message(message: dict[str, Any]) -> bool:
    text = str(message.get("messageText", ""))
    return message.get("messageType") in KEPT_MESSAGE_TYPES or text.startswith(VERSION_PREFIX)


def compact_cbmc_stdout(stdout: str) -> str:
    kept = []
    for message in json.loads(stdout):
        if "result" in message:
            kept.append({"result": [_compact_result(result) for result in message["result"] if _recorded(result)]})
        elif "messageText" in message:
            if _kept_message(message):
                kept.append(message)
        else:
            kept.append(message)
    return json.dumps(kept)


def record_gate(ctx: BackendContext) -> tuple[StageResult, list[dict[str, Any]]]:
    recorder = Recorder(ctx)
    ctx.run = recorder
    result = run_inductive(ctx)
    for entry, label in zip(recorder.entries, result.details["argv"]):
        entry["label"] = label
        if label in CBMC_LABELS:
            entry["stdout"] = to_placeholders(ctx, compact_cbmc_stdout(entry["stdout"]))
    return result, recorder.entries


def load_gate_recording() -> list[dict[str, Any]]:
    return load_recording() + load_recording(TALLY_CBMC_RECORDING)


def replay_gate(ctx: BackendContext, entries: list[dict[str, Any]]) -> tuple[StageResult, Replayer]:
    replayer = Replayer(ctx, entries)
    ctx.run = replayer
    return run_inductive(ctx), replayer


def results_edit(transform: Callable[[Results], None]) -> Callable[[dict[str, Any]], None]:
    def change(entry: dict[str, Any]) -> None:
        messages = json.loads(entry["stdout"])
        transform(next(message["result"] for message in messages if "result" in message))
        entry["stdout"] = json.dumps(messages)

    return change


def messages_edit(transform: Callable[[list[dict[str, Any]]], None]) -> Callable[[dict[str, Any]], None]:
    def change(entry: dict[str, Any]) -> None:
        messages = json.loads(entry["stdout"])
        transform(messages)
        entry["stdout"] = json.dumps(messages)

    return change


def codes(result: StageResult) -> list[Reason]:
    return [code.code for code in stage_reason_codes(result)]


def supplied_codes(result: StageResult) -> list[Reason]:
    return [Reason(entry["code"]) for entry in result.details["reason_codes"]]


def with_description(results: Results, description: str) -> list[dict[str, Any]]:
    return [result for result in results if result.get("description") == description]


def with_class(results: Results, property_class: str) -> list[dict[str, Any]]:
    return [result for result in results if f".{property_class}." in str(result.get("property"))]


def write_gate_recording(entries: list[dict[str, Any]], path: Path = TALLY_CBMC_RECORDING) -> None:
    cbmc_entries = [entry for entry in entries if entry["label"] in CBMC_LABELS]
    path.write_text(json.dumps(cbmc_entries, indent=1, sort_keys=True) + "\n", encoding="utf-8")
