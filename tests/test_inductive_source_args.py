from __future__ import annotations

import os

import pytest
from cbmc_support import _fake_tools, make_cbmc_project

from leanc.backends.cbmc.argv import CHECK_FLAGS, VACUITY_DEFINE, build_cbmc_argv, cbmc_source_args
from leanc.backends.inductive.preprocess import PREPROCESS_FLAG, preprocess_argv
from leanc.toolchain.context import BackendContext

CBMC = "/opt/cbmc/bin/cbmc"
EDITABLE = "src/impl.c"
HARNESS = "harness/h.c"
COMPILE_FLAGS = ["-std=gnu11", "-funsigned-char", "-DWIDTH=8", "-UOLD", "-Iextra", "-Wall"]


@pytest.fixture
def ctx(tmp_path) -> BackendContext:
    context = make_cbmc_project(tmp_path, src="add", harness="add_harness", function="add", compile_defines=["MODE=2", "NDEBUG"])
    context.spec.compile.flags = list(COMPILE_FLAGS)
    _fake_tools(context, CBMC)
    return context


def test_source_args_are_exactly_what_the_bounded_gate_passes(ctx):
    include_dir = os.path.abspath(ctx.include_dir)
    expected = ["-Iextra", "-Iinclude", f"-I{include_dir}", "-DWIDTH=8", "-DMODE=2", "--c11", "--unsigned-char"]
    assert cbmc_source_args(ctx) == expected


@pytest.mark.parametrize("mode", ["verify", "vacuity", "coverage"])
def test_bounded_argv_embeds_the_source_args_unchanged(ctx, mode):
    argv = build_cbmc_argv(ctx, mode=mode)
    source_args = cbmc_source_args(ctx)
    assert argv[:3] == [CBMC, EDITABLE, HARNESS]
    assert argv[3 : 3 + len(source_args)] == source_args


def test_bounded_verify_argv_is_byte_identical(ctx):
    include_dir = os.path.abspath(ctx.include_dir)
    assert build_cbmc_argv(ctx, mode="vacuity") == [
        CBMC,
        EDITABLE,
        HARNESS,
        "-Iextra",
        "-Iinclude",
        f"-I{include_dir}",
        "-DWIDTH=8",
        "-DMODE=2",
        "--c11",
        "--unsigned-char",
        VACUITY_DEFINE,
        "--unwind",
        "5",
        "--unwinding-assertions",
        "--no-self-loops-to-assumptions",
        "--json-ui",
    ]


def test_bounded_verify_argv_with_memory_model_and_unwindset_is_byte_identical(tmp_path):
    context = make_cbmc_project(
        tmp_path, src="add", harness="add_harness", function="add", object_bits=12, unwindset={"add.0": 3}
    )
    _fake_tools(context, CBMC)
    include_dir = os.path.abspath(context.include_dir)
    checks = [arg for arg in build_cbmc_argv(context, mode="verify") if arg.endswith("-check")]
    assert build_cbmc_argv(context, mode="verify") == [
        CBMC,
        EDITABLE,
        HARNESS,
        "-Iinclude",
        f"-I{include_dir}",
        "--c11",
        *checks,
        "--unwind",
        "5",
        "--unwindset",
        "add.0:3",
        "--unwinding-assertions",
        "--no-self-loops-to-assumptions",
        "--object-bits",
        "12",
        "--json-ui",
    ]
    assert checks == [CHECK_FLAGS[check] for check in context.spec.effective_cbmc_checks()]


@pytest.mark.parametrize("vacuity", [False, True])
def test_preprocessing_uses_the_bounded_source_args(ctx, vacuity):
    argv = preprocess_argv(ctx, HARNESS, vacuity=vacuity)
    assert argv == [CBMC, PREPROCESS_FLAG, *cbmc_source_args(ctx), *([VACUITY_DEFINE] if vacuity else []), HARNESS]
