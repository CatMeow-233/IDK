from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from inductive_build_support import (
    BOUNDED_HARNESS,
    CONTRACTS,
    INDUCTIVE_HARNESS,
    PIPELINE_FIXTURES,
    TALLY,
    TALLY_SOURCE,
    tally_context,
    tally_spec_data,
)
from inductive_gate_support import codes

from leanc.backends.inductive import run_inductive
from leanc.foundation.models import FailureReport, StageResult
from leanc.foundation.reasons import Reason
from leanc.specs.model import Spec
from leanc.toolchain.context import BackendContext, VerifyOptions, make_context

BELOW = ("tally_below",)
RETURN_ONLY_HARNESS = "return_only.c"
BELOW_INVARIANT = ("0 <= i", "i <= n", "0 <= c", "c <= i")
BELOW_ASSIGNS = ("i", "c")
BELOW_DECREASES = "n - i"
TALLY_LOOPS = TALLY / "loops.json"
RING = PIPELINE_FIXTURES / "ring"
RING_PREDICATES = PIPELINE_FIXTURES / "ring_pred"
RING_SOURCE = "src/ring.c"
RING_HEADER = "include/ring.h"
RING_FUNCTION = "ring_add"
RING_SIGNATURE = "void ring_add(struct ring *r, unsigned char c)"
NO_LOOPS = json.dumps({"loops": []})
UNPROVED_KIND = "unproved"


def below_contract(line: int, **fields: Any) -> str:
    entry: dict[str, Any] = {
        "file": TALLY_SOURCE,
        "line": line,
        "invariant": list(BELOW_INVARIANT),
        "assigns": list(BELOW_ASSIGNS),
        "decreases": BELOW_DECREASES,
    }
    entry.update(fields)
    return json.dumps({"loops": [entry]})


def shifted_tally_contracts(shift: int) -> str:
    loops = json.loads(TALLY_LOOPS.read_text(encoding="utf-8"))["loops"]
    return json.dumps({"loops": [{**loop, "line": loop["line"] + shift} for loop in loops]})


def below_context(
    tmp_path: Path,
    contracts: str,
    *,
    source: str | None = None,
    harness: str = RETURN_ONLY_HARNESS,
    options: VerifyOptions | None = None,
    extra: dict[str, str] | None = None,
) -> BackendContext:
    project: dict[str, Any] = {"harness": harness, "contracts": contracts, "extra": extra}
    if source is not None:
        project["source"] = TALLY / "src" / source
    return tally_context(tmp_path, data=tally_spec_data(BELOW), options=options, **project)


def below_gate(tmp_path: Path, contracts: str, **project: Any) -> StageResult:
    return run_inductive(below_context(tmp_path, contracts, **project))


def ring_spec_data() -> dict[str, Any]:
    return {
        "spec_id": "ring-inductive",
        "version": 1,
        "title": "ring inductive gate",
        "requirement": "test",
        "target": {"function": RING_FUNCTION, "signature": RING_SIGNATURE, "returns": {"type": "void"}},
        "sources": {"editable": [RING_SOURCE], "headers": [RING_HEADER], "include_dirs": ["include"]},
        "verification": {"harness": BOUNDED_HARNESS, "unwind": 4, "timeout_s": 120},
        "inductive": {"harness": INDUCTIVE_HARNESS, "contracts": CONTRACTS, "functions": [RING_FUNCTION]},
        "gates": ["compile", "cbmc"],
    }


def ring_gate(tmp_path: Path, harness: str, *, fixture: Path = RING, source: str = RING_SOURCE) -> StageResult:
    root = tmp_path / "proj"
    files = {
        RING_SOURCE: fixture / source,
        RING_HEADER: fixture / RING_HEADER,
        INDUCTIVE_HARNESS: fixture / "harness" / harness,
    }
    for rel, source in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, root / rel)
    (root / BOUNDED_HARNESS).write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (root / CONTRACTS).parent.mkdir(parents=True, exist_ok=True)
    (root / CONTRACTS).write_text(NO_LOOPS, encoding="utf-8")
    spec = Spec.model_validate(ring_spec_data())
    return run_inductive(make_context(spec, root, tmp_path / "run", VerifyOptions(sandbox_preference="none")))


def failing(result: StageResult, property_prefix: str) -> list[FailureReport]:
    return [failure for failure in result.failures if (failure.property_id or "").startswith(property_prefix)]


def described(result: StageResult, text: str) -> list[FailureReport]:
    return [failure for failure in result.failures if text in failure.description]


def only_codes(result: StageResult) -> set[Reason]:
    return set(codes(result))


def never_counterexamples(result: StageResult) -> bool:
    return all(failure.kind == UNPROVED_KIND and not failure.inputs for failure in result.failures)
