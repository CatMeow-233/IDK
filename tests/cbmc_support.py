from __future__ import annotations

import shutil
from pathlib import Path

from leanc.specs.model import Spec
from leanc.toolchain.context import BackendContext, make_context
from leanc.toolchain.environment import Toolchain

FIX = Path(__file__).resolve().parent / "fixtures" / "cbmc"

FUNCTIONS = {
    "arr_get": "int arr_get(const int *arr, size_t len, size_t idx, int *out)",
    "avg": "int avg(int total, int count)",
    "add": "int add(int a, int b)",
    "deref": "int deref(const int *p)",
    "count_zeros": "size_t count_zeros(const int *a, size_t n)",
    "mul_div": "uint64_t mul_div(uint64_t a, uint64_t b)",
    "point_ratio": "int point_ratio(const struct point *p, int k)",
}


def make_cbmc_project(
    tmp_path: Path,
    *,
    src: str,
    harness: str,
    function: str,
    compile_defines: list[str] | None = None,
    **verification,
) -> BackendContext:
    root = tmp_path / "proj"
    for d in ("include", "src", "harness"):
        (root / d).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIX / "include" / "cbmc_fx.h", root / "include" / "cbmc_fx.h")
    shutil.copyfile(FIX / "src" / f"{src}.c", root / "src" / "impl.c")
    shutil.copyfile(FIX / "harness" / f"{harness}.c", root / "harness" / "h.c")
    data = {
        "spec_id": "cbmc-unit",
        "version": 1,
        "title": "cbmc unit test",
        "requirement": "test",
        "target": {"function": function, "signature": FUNCTIONS[function], "returns": {"type": "int"}},
        "sources": {"editable": ["src/impl.c"], "headers": ["include/cbmc_fx.h"], "include_dirs": ["include"]},
        "compile": {"defines": compile_defines or []},
        "verification": {"harness": "harness/h.c", "unwind": 5, "timeout_s": 120, **verification},
        "gates": ["compile", "cbmc"],
    }
    spec = Spec.model_validate(data)
    return make_context(spec, root, tmp_path / "run")


def _fake_tools(ctx: BackendContext, cbmc: str | None = "/opt/cbmc/bin/cbmc") -> None:
    ctx.tools = Toolchain(clang=None, cbmc=cbmc, clang_version=None, cbmc_version="5.95.1 (cbmc-5.95.1)" if cbmc else None)
