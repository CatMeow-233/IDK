from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from leanc.backends.cbmc.argv import CHECK_FLAGS
from leanc.specs.model import MANDATORY_CHECKS
from leanc.toolchain.context import leanc_include_dir, leanc_runtime_dir

TOOL_TIMEOUT_S = 120
CBMC_BANNER = "CBMC version"
DFCC_ENTRY = "main"
DEFAULT_UNWIND = 4
MANDATORY_CHECK_FLAGS = tuple(CHECK_FLAGS[check] for check in MANDATORY_CHECKS)
INDUCTIVE_MODELS = leanc_runtime_dir() / "inductive_models.c"
PROPERTY_FAILED = "FAILURE"
PROPERTY_HOLDS = "SUCCESS"
ERROR_MESSAGE_TYPE = "ERROR"
LOOP_PROPERTY_CLASSES = (
    "loop_invariant_base",
    "loop_invariant_step",
    "loop_decreases",
    "loop_assigns",
    "loop_step_unwinding",
)
IGNORED_CLAUSE_WARNING = "ignoring"
PROPERTY_ID_PARTS = 3
MAIN_WRAPPER = "void {entry}(void);\nint main(void)\n{{\n    {entry}();\n    return 0;\n}}\n"


def tool(name: str) -> str:
    found = shutil.which(name)
    assert found, f"{name} is installed with cbmc"
    return found


def run_tool(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=TOOL_TIMEOUT_S, check=False)


def include_args(*dirs: str) -> list[str]:
    return [*(f"-I{d}" for d in dirs), f"-I{leanc_include_dir()}"]


def cbmc_preprocessed(cwd: Path, rel: str, *args: str) -> str:
    done = run_tool([tool("cbmc"), "--preprocess", rel, *args], cwd)
    assert done.returncode == 0, done.stderr
    banner, _, text = done.stdout.partition("\n")
    assert banner.startswith(CBMC_BANNER), banner
    return text


def main_wrapper(entry: str) -> str:
    return MAIN_WRAPPER.format(entry=entry)


def linked(cwd: Path, units: list[str], out: str = "prog.goto") -> str:
    done = run_tool([tool("goto-cc"), "-o", out, *units], cwd)
    assert done.returncode == 0, done.stdout + done.stderr
    return out


def instrumented(cwd: Path, goto: str, out: str = "dfcc.goto") -> subprocess.CompletedProcess[str]:
    done = run_tool([tool("goto-instrument"), "--dfcc", DFCC_ENTRY, "--apply-loop-contracts", goto, out], cwd)
    assert done.returncode == 0, done.stdout + done.stderr
    return done


def cbmc_results(cwd: Path, goto: str, *flags: str, unwind: int = DEFAULT_UNWIND) -> list[dict[str, Any]]:
    argv = [tool("cbmc"), goto, "--no-library", *flags, "--unwind", str(unwind), "--unwinding-assertions", "--json-ui"]
    done = run_tool(argv, cwd)
    messages = json.loads(done.stdout)
    errors = [m for m in messages if isinstance(m, dict) and m.get("messageType") == ERROR_MESSAGE_TYPE]
    assert not errors, errors
    results = [m["result"] for m in messages if isinstance(m, dict) and "result" in m]
    assert len(results) == 1, done.stdout[-2000:]
    return results[0]


def dfcc_results(cwd: Path, units: list[str], *flags: str, unwind: int = DEFAULT_UNWIND) -> list[dict[str, Any]]:
    instrumented(cwd, linked(cwd, units))
    return cbmc_results(cwd, "dfcc.goto", *flags, unwind=unwind)


def failed(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [result for result in results if result["status"] != PROPERTY_HOLDS]


def described(results: list[dict[str, Any]], description: str) -> list[dict[str, Any]]:
    return [result for result in results if result["description"] == description]


def _property_class(result: dict[str, Any]) -> str:
    parts = result["property"].split(".")
    return parts[1] if len(parts) == PROPERTY_ID_PARTS else ""


def loop_properties(results: list[dict[str, Any]], loop: str) -> dict[str, set[str]]:
    classes: dict[str, set[str]] = {}
    for result in results:
        property_class = _property_class(result)
        if property_class in LOOP_PROPERTY_CLASSES and result["description"].endswith(f"for loop {loop}"):
            classes.setdefault(property_class, set()).add(result["status"])
    return classes
