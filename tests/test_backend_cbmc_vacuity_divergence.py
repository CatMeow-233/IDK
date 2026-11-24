from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from cbmc_replay_support import DIVERGENCE_PREPROCESS_RUNS
from cbmc_support import FIX, make_cbmc_project

from leanc.backends.cbmc import run_cbmc
from leanc.backends.cbmc.execution import cbmc_limits
from leanc.backends.cbmc.preprocess import PREPROCESS_FLAG, preprocess_argv, without_banner
from leanc.backends.cbmc.program_inputs import ProgramInputs, prepare_inputs
from leanc.backends.cbmc.vacuity_divergence import DIVERGENCE_RULE, DIVERGENCE_RULE_ID, vacuity_divergence
from leanc.foundation.status import Outcome, Status
from leanc.specs.loader import load_spec
from leanc.specs.model import Spec
from leanc.toolchain.context import BackendContext, make_context

pytestmark = pytest.mark.usefixtures("needs_cbmc")

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
ENOUGH_UNWIND = 10
VACUITY_ONLY_HEADER = "include/vacuity_only.h"
DIVERGING_HARNESSES = {
    "count_zeros_vacuity_define_harness": "harness/h.c:16",
    "count_zeros_vacuity_stringize_harness": "harness/h.c:18",
}


def _context(tmp_path: Path, harness: str, src: str = "count_zeros") -> BackendContext:
    return make_cbmc_project(tmp_path, src=src, harness=harness, function="count_zeros", unwind=ENOUGH_UNWIND)


def _with_editable_only_header(tmp_path: Path) -> BackendContext:
    base = _context(tmp_path, "count_zeros_harness", src="count_zeros_vacuity_header")
    shutil.copyfile(FIX / VACUITY_ONLY_HEADER, base.project_root / VACUITY_ONLY_HEADER)
    data = base.spec.model_dump(mode="json")
    data["sources"]["headers"].append(VACUITY_ONLY_HEADER)
    return make_context(Spec.model_validate(data), base.project_root, tmp_path / "run-header")


def _c_example_specs() -> list[Path]:
    return [path for path in sorted(EXAMPLES.glob("*/spec*.yaml")) if load_spec(path).language.name == "c"]


def _preprocessed_harness(ctx: BackendContext, harness: str, vacuity: bool) -> str | None:
    proc = ctx.run(preprocess_argv(ctx, harness, vacuity=vacuity), limits=cbmc_limits(ctx), cwd=ctx.snapshot_dir.resolve())
    return without_banner(proc.stdout)


def test_the_control_harness_passes_after_both_preprocessings_of_each_unit(tmp_path):
    result = run_cbmc(_context(tmp_path, "count_zeros_harness"))
    assert result.outcome is Outcome.PASS, result.summary
    preprocessed = [invocation for invocation in result.invocations if PREPROCESS_FLAG in invocation.argv]
    assert len(preprocessed) == DIVERGENCE_PREPROCESS_RUNS
    assert all(invocation.exit_code == 0 for invocation in preprocessed)


@pytest.mark.parametrize("harness, where", sorted(DIVERGING_HARNESSES.items()), ids=["define", "stringize"])
def test_a_harness_that_differs_with_the_vacuity_define_is_an_error_without_a_vacuity_run(tmp_path, harness, where):
    result = run_cbmc(_context(tmp_path, harness))
    assert result.outcome is Outcome.ERROR and result.status is Status.TOOL_ERROR, result.summary
    assert f"the harness harness/h.c {DIVERGENCE_RULE}: {where}:" in result.summary
    assert "vacuity" not in result.details["argv"] and result.details["vacuity"]["status"] == "skipped"
    (diagnostic,) = [d for d in result.diagnostics if d.rule_id == DIVERGENCE_RULE_ID]
    assert f"{diagnostic.location.file}:{diagnostic.location.line}" == where


def test_an_editable_source_that_differs_with_the_vacuity_define_is_an_error(tmp_path):
    result = run_cbmc(_with_editable_only_header(tmp_path))
    assert result.outcome is Outcome.ERROR, result.summary
    assert f"the editable source src/impl.c {DIVERGENCE_RULE}: src/impl.c:8:" in result.summary


@pytest.mark.parametrize("spec_path", _c_example_specs(), ids=lambda path: f"{path.parent.name}/{path.name}")
def test_every_example_harness_preprocesses_to_the_same_program_in_both_builds(tmp_path, spec_path):
    spec = load_spec(spec_path)
    ctx = make_context(spec, spec_path.parent, tmp_path / "run")
    invocations = []
    inputs = prepare_inputs(ctx, cbmc_limits(ctx), invocations, with_vacuity=True)
    assert isinstance(inputs, ProgramInputs), inputs.summary
    assert len(invocations) == 2 * (len(spec.sources.editable) + 1)
    assert inputs.sites and all(site.origin is not None for site in inputs.sites)
    if spec.inductive is not None:
        verify, vacuity = (_preprocessed_harness(ctx, spec.inductive.harness, mode) for mode in (False, True))
        assert verify is not None and vacuity is not None and vacuity != verify
        assert vacuity_divergence(verify, vacuity) is None
