from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from leanc.backends.inductive import InductiveBuild, build_inductive
from leanc.foundation.runner import ProcResult
from leanc.toolchain.context import BackendContext, leanc_include_dir, leanc_runtime_dir

RECORDINGS = Path(__file__).resolve().parent / "fixtures" / "inductive" / "json"
TALLY_RECORDING = RECORDINGS / "tally_build.json"
RUNTIME_PLACEHOLDER = "@LEANC_RUNTIME@"
INCLUDE_PLACEHOLDER = "@LEANC_INCLUDE@"
RUN_PLACEHOLDER = "@RUN@"
SYMBOL_TABLE_KEY = "symbolTable"
OUTPUT_FLAG = "-o"
GOTO_CC = "goto-cc"
DFCC_FLAG = "--dfcc"
KEPT_SYMBOL_FIELDS = ("baseName", "module", "isFileLocal", "isStaticLifetime", "isExtern", "isType")
KEPT_LOCATION_FIELDS = ("file", "line")
RECORDED_FIELDS = ("exit_code", "signal", "timed_out", "output_truncated", "stdout", "stderr")
NO_OUTPUT_FILE = "no_output_file"
START_ERROR = "error"


def _real(path: Path) -> str:
    return os.path.realpath(path)


def _placeholders(ctx: BackendContext) -> list[tuple[str, str]]:
    return [
        (_real(leanc_runtime_dir()), RUNTIME_PLACEHOLDER),
        (_real(leanc_include_dir()), INCLUDE_PLACEHOLDER),
        (os.path.abspath(ctx.run_dir), RUN_PLACEHOLDER),
    ]


def to_placeholders(ctx: BackendContext, text: str) -> str:
    for real, placeholder in _placeholders(ctx):
        text = text.replace(real, placeholder)
    return text


def from_placeholders(ctx: BackendContext, text: str) -> str:
    for real, placeholder in _placeholders(ctx):
        text = text.replace(placeholder, real)
    return text


def _named_id(node: Any, key: str) -> dict[str, Any]:
    named = node.get("namedSub", {}) if isinstance(node, dict) else {}
    return {"id": named[key]["id"]} if isinstance(named.get(key), dict) and "id" in named[key] else {}


def _compact_symbol(entry: dict[str, Any]) -> dict[str, Any]:
    compact = {key: entry[key] for key in KEPT_SYMBOL_FIELDS if key in entry}
    location = entry.get("location", {})
    compact["location"] = {"id": "", "namedSub": {key: _named_id(location, key) for key in KEPT_LOCATION_FIELDS if _named_id(location, key)}}
    compact["value"] = {"id": entry.get("value", {}).get("id", "")}
    symbol_type = entry.get("type", {})
    compact["type"] = {"id": symbol_type.get("id", "")}
    return_type = _named_id(symbol_type, "return_type")
    if return_type:
        compact["type"]["namedSub"] = {"return_type": return_type}
    return compact


def compact_stdout(stdout: str) -> str:
    if SYMBOL_TABLE_KEY not in stdout:
        return stdout
    messages = json.loads(stdout)
    for message in messages:
        if isinstance(message, dict) and SYMBOL_TABLE_KEY in message:
            message[SYMBOL_TABLE_KEY] = {name: _compact_symbol(entry) for name, entry in message[SYMBOL_TABLE_KEY].items()}
    return json.dumps(messages)


@dataclass
class Recorder:
    ctx: BackendContext
    entries: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, argv: list[str], **kwargs: Any) -> ProcResult:
        proc = BackendContext.run(self.ctx, argv, **kwargs)
        entry = {"tool": Path(argv[0]).name, **{key: getattr(proc, key) for key in RECORDED_FIELDS}}
        entry["stdout"] = to_placeholders(self.ctx, compact_stdout(proc.stdout))
        entry["stderr"] = to_placeholders(self.ctx, proc.stderr)
        self.entries.append(entry)
        return proc


def record_build(ctx: BackendContext) -> tuple[InductiveBuild, list[dict[str, Any]]]:
    recorder = Recorder(ctx)
    ctx.run = recorder
    result = build_inductive(ctx)
    for entry, label in zip(recorder.entries, result.argv):
        entry["label"] = label
    return result, recorder.entries


def load_recording(path: Path = TALLY_RECORDING) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_recording(entries: list[dict[str, Any]], path: Path = TALLY_RECORDING) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _output_path(argv: list[str]) -> Path | None:
    if Path(argv[0]).name == GOTO_CC and OUTPUT_FLAG in argv:
        return Path(argv[argv.index(OUTPUT_FLAG) + 1])
    if DFCC_FLAG in argv:
        return Path(argv[-1])
    return None


@dataclass
class Replayer:
    ctx: BackendContext
    entries: list[dict[str, Any]]
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: list[str], **kwargs: Any) -> ProcResult:
        entry = self.entries[len(self.calls)]
        self.calls.append(list(argv))
        assert Path(argv[0]).name == entry["tool"], (argv[0], entry["label"])
        output = _output_path(argv)
        if output is not None and entry["exit_code"] == 0 and not entry.get(NO_OUTPUT_FILE):
            output.write_bytes(b"")
        return ProcResult(
            argv=list(argv),
            cwd=str(kwargs.get("cwd", "")),
            exit_code=entry["exit_code"],
            signal=entry["signal"],
            timed_out=entry["timed_out"],
            output_truncated=entry["output_truncated"],
            duration_s=0.0,
            stdout=from_placeholders(self.ctx, entry["stdout"]),
            stderr=from_placeholders(self.ctx, entry["stderr"]),
            error=entry.get(START_ERROR),
        )


Mutation = Callable[[dict[str, Any]], None]


def mutated(entries: list[dict[str, Any]], label: str, change: Mutation) -> list[dict[str, Any]]:
    copied = json.loads(json.dumps(entries))
    matches = [entry for entry in copied if entry["label"] == label]
    assert len(matches) == 1, f"no single recorded step {label!r}"
    change(matches[0])
    return copied


def replay_build(ctx: BackendContext, entries: list[dict[str, Any]]) -> tuple[InductiveBuild, Replayer]:
    replayer = Replayer(ctx, entries)
    ctx.run = replayer
    return build_inductive(ctx), replayer
