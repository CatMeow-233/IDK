from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from goto_pipeline_support import MANDATORY_CHECK_FLAGS, PROPERTY_HOLDS, tool

from leanc.backends.inductive import InductiveBuild, build_inductive
from leanc.specs.model import Spec
from leanc.toolchain.context import BackendContext, VerifyOptions, make_context

PIPELINE_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "inductive" / "pipeline"
TALLY = PIPELINE_FIXTURES / "tally"
TALLY_SOURCE = "src/tally.c"
TALLY_HEADER = "include/tally.h"
INDUCTIVE_HARNESS = "harness/inductive.c"
BOUNDED_HARNESS = "harness/bounded.c"
CONTRACTS = "inductive/loops.json"
TALLY_FUNCTIONS = ("tally_below", "tally_max", "tally_clear")
TALLY_SIGNATURE = "int tally_below(const int *a, int n, int x)"
CBMC_TIMEOUT_S = 300
VERIFY_UNWIND = "4"
VACUITY_UNWIND = "4"


def tally_spec_data(functions: tuple[str, ...] = TALLY_FUNCTIONS, **verification: Any) -> dict[str, Any]:
    return {
        "spec_id": "tally-inductive",
        "version": 1,
        "title": "tally inductive build",
        "requirement": "test",
        "target": {"function": functions[0], "signature": TALLY_SIGNATURE, "returns": {"type": "int"}},
        "sources": {"editable": [TALLY_SOURCE], "headers": [TALLY_HEADER], "include_dirs": ["include"]},
        "verification": {"harness": BOUNDED_HARNESS, "unwind": 4, "timeout_s": 120, **verification},
        "inductive": {"harness": INDUCTIVE_HARNESS, "contracts": CONTRACTS, "functions": list(functions)},
        "gates": ["compile", "cbmc"],
    }


def write_project(
    root: Path,
    *,
    harness: str = "branches.c",
    source: Path | None = None,
    contracts: str | None = None,
    extra: dict[str, str] | None = None,
) -> Path:
    for directory in ("src", "include", "harness", "inductive"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source or TALLY / TALLY_SOURCE, root / TALLY_SOURCE)
    shutil.copyfile(TALLY / TALLY_HEADER, root / TALLY_HEADER)
    shutil.copyfile(TALLY / "harness" / harness, root / INDUCTIVE_HARNESS)
    (root / BOUNDED_HARNESS).write_text("int main(void) { return 0; }\n", encoding="utf-8")
    text = (TALLY / "loops.json").read_text(encoding="utf-8") if contracts is None else contracts
    (root / CONTRACTS).write_text(text, encoding="utf-8")
    for rel, content in (extra or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content, encoding="utf-8")
    return root


def tally_context(
    tmp_path: Path,
    *,
    data: dict[str, Any] | None = None,
    options: VerifyOptions | None = None,
    **project: Any,
) -> BackendContext:
    root = write_project(tmp_path / "proj", **project)
    spec = Spec.model_validate(data or tally_spec_data())
    return make_context(spec, root, tmp_path / "run", options or VerifyOptions(sandbox_preference="none"))


CAPTURE = PIPELINE_FIXTURES / "capture"
CAPTURE_SOURCE = "src/ed.c"
CAPTURE_HEADER = "include/cap.h"
CAPTURE_HARNESS = "harness/h.c"
CAPTURE_FUNCTION = "use_ed"


def capture_context(tmp_path: Path) -> BackendContext:
    root = tmp_path / "proj"
    for rel in (CAPTURE_SOURCE, CAPTURE_HEADER, CAPTURE_HARNESS):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CAPTURE / rel, root / rel)
    data = tally_spec_data((CAPTURE_FUNCTION,))
    data["target"] = {"function": CAPTURE_FUNCTION, "signature": "int use_ed(void)", "returns": {"type": "int"}}
    data["sources"] = {"editable": [CAPTURE_SOURCE], "headers": [CAPTURE_HEADER], "include_dirs": ["include"]}
    data["inductive"]["harness"] = CAPTURE_HARNESS
    return make_context(Spec.model_validate(data), root, tmp_path / "run", VerifyOptions(sandbox_preference="none"))


def no_contracts() -> str:
    return json.dumps({"loops": []})


def built(ctx: BackendContext) -> InductiveBuild:
    return build_inductive(ctx)


def cbmc_properties(program: Path, *, checks: bool) -> dict[str, list[str]]:
    flags = list(MANDATORY_CHECK_FLAGS) if checks else []
    unwind = VERIFY_UNWIND if checks else VACUITY_UNWIND
    argv = [tool("cbmc"), str(program), "--no-library", *flags, "--unwind", unwind, "--unwinding-assertions", "--json-ui"]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=CBMC_TIMEOUT_S, check=False)
    statuses: dict[str, list[str]] = {}
    for message in json.loads(done.stdout):
        if isinstance(message, dict) and "result" in message:
            for result in message["result"]:
                statuses.setdefault(result["description"], []).append(result["status"])
    return statuses


def holds(statuses: dict[str, list[str]], description: str) -> bool:
    return bool(statuses.get(description)) and all(status == PROPERTY_HOLDS for status in statuses[description])


def reached(statuses: dict[str, list[str]], description: str) -> bool:
    return any(status != PROPERTY_HOLDS for status in statuses.get(description, []))
