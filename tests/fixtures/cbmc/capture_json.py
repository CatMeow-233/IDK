"""Regenerate the captured CBMC outputs in ``json/`` (not a test).

Usage (from the repository root, with CBMC installed)::

    python tests/fixtures/cbmc/capture_json.py

Each scenario is assembled exactly like ``tests/cbmc_support.py`` does
(``src/impl.c`` + ``harness/h.c``) and run through the real gate; the raw
``--json-ui`` output it saved is copied here.  The only edits are cosmetic:
``workingDirectory`` (an absolute temporary path) is replaced by
``/work/snapshot`` and the JSON is re-serialised compactly.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
sys.path.insert(0, str(REPO / "tests"))

from cbmc_support import make_cbmc_project

from leanc.backends.cbmc import build_cbmc_argv, run_cbmc
from leanc.foundation.runner import Limits

# (output name, src, harness, function, verification overrides, modes to keep)
SCENARIOS = [
    ("arr_get_ok", "arr_get_ok", "arr_get_harness", "arr_get", {}, ["verify", "vacuity"]),
    ("arr_get_off_by_one", "arr_get_off_by_one", "arr_get_harness", "arr_get", {}, ["verify"]),
    ("avg", "avg", "avg_harness", "avg", {}, ["verify"]),
    ("add", "add", "add_harness", "add", {}, ["verify"]),
    ("deref", "deref", "deref_harness", "deref", {}, ["verify"]),
    ("count_zeros_unwind3", "count_zeros", "count_zeros_harness", "count_zeros", {"unwind": 3}, ["verify"]),
    ("point", "point", "point_harness", "point_ratio", {}, ["verify"]),
    ("arr_get_contradictory", "arr_get_ok", "arr_get_contradictory_harness", "arr_get", {}, ["vacuity"]),
    ("syntax_error", "syntax_error", "arr_get_harness", "arr_get", {}, ["verify"]),
    (
        "count_zeros_dead_code",
        "count_zeros_dead_code",
        "count_zeros_harness",
        "count_zeros",
        {"unwind": 9, "coverage": True},
        ["coverage"],
    ),
]


def _sanitize(text: str) -> str:
    data = json.loads(text)

    def walk(x):
        if isinstance(x, dict):
            return {k: ("/work/snapshot" if k == "workingDirectory" else walk(v)) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x

    return json.dumps(walk(data), separators=(",", ":")) + "\n"


def main() -> None:
    out_dir = HERE / "json"
    out_dir.mkdir(exist_ok=True)
    for name, src, harness, function, over, modes in SCENARIOS:
        with tempfile.TemporaryDirectory() as td:
            ctx = make_cbmc_project(Path(td), src=src, harness=harness, function=function, **over)
            res = run_cbmc(ctx)
            for mode in modes:
                raw = ctx.raw_dir / f"cbmc-{mode}.json"
                (out_dir / f"{name}.{mode}.json").write_text(_sanitize(raw.read_text()))
            print(f"{name}: {res.outcome.value}: {res.summary[:100]}")
    # Out of memory: the SAT solver of the hard mul_div problem under a 60 MB
    # address-space limit (below the spec minimum, hence run directly).
    with tempfile.TemporaryDirectory() as td:
        ctx = make_cbmc_project(Path(td), src="mul_div", harness="mul_div_harness", function="mul_div")
        argv = build_cbmc_argv(ctx, mode="verify")
        proc = ctx.run(argv, limits=Limits(timeout_s=120, memory_mb=60, max_output_bytes=64 << 20))
        (out_dir / "mul_div_oom.verify.json").write_text(_sanitize(proc.stdout))
        print(f"mul_div_oom: exit {proc.exit_code}")


if __name__ == "__main__":
    main()
