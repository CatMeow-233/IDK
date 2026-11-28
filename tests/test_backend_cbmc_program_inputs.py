from __future__ import annotations

from pathlib import Path

import pytest
from cbmc_support import make_cbmc_project

from leanc.backends.cbmc import run_cbmc
from leanc.backends.cbmc.preprocess import NOT_UTF8_OUTPUT, SILENT_PREPROCESSING_FAILURE
from leanc.backends.cbmc.program_inputs import BUILD_SUBDIR
from leanc.backends.cbmc.symbol_table import CPROVER_DECLARATION_RULE
from leanc.backends.cbmc.vacuity_sites import SITE_ID_INFIX
from leanc.foundation.status import Outcome, Status
from leanc.toolchain.context import BackendContext

pytestmark = pytest.mark.usefixtures("needs_cbmc")

ENOUGH_UNWIND = 10
RAW_VERIFY_OUTPUT = "../../raw/cbmc-verify.json"
PINNED_DATE_TIME = '"Jan  1 1970" " " "00:00:00"'
PREPROCESSED_HARNESS = "harness/h.i"
SHADOW_FORMS = ["param", "local", "function_param", "file_scope", "file_object"]
MODE_DIRS = {"verify": "verify", "vacuity": "vacuity", "coverage": "verify"}
EDITABLE = "src/impl.c"
HARNESS = "harness/h.c"
ABSENT_HEADER = "leanc_absent_project_header.h"
MISSING_TAIL_INCLUDE = f'#include "{ABSENT_HEADER}"\n'.encode()
ERROR_DIRECTIVE = b"#error leanc stops here\n"
LATIN1_LITERAL = b'static const char leanc_tag[] = "\xe9";\n'
SHADOWING_STRING_H = "#include_next <string.h>\n#define __CPROVER_precondition(c, m) ((void)0)\n"
OVERLAPPING_COPY = (
    '#include <string.h>\n#include "cbmc_fx.h"\n\n'
    "int add(int a, int b)\n{\n    char buf[8] = {0};\n    memcpy(buf + 1, buf, 4);\n    return (a & 1) + (b & 1) + buf[0];\n}\n"
)


def _context(tmp_path: Path, harness: str, **verification) -> BackendContext:
    return make_cbmc_project(
        tmp_path, src="count_zeros", harness=harness, function="count_zeros", unwind=ENOUGH_UNWIND, **verification
    )


def _units(argv: list[str]) -> list[str]:
    return [arg for arg in argv if arg.endswith(".i")]


def test_every_cbmc_run_reads_only_the_preprocessed_units_of_its_mode(tmp_path):
    ctx = _context(tmp_path, "count_zeros_harness", coverage=True)
    result = run_cbmc(ctx)
    assert result.outcome is Outcome.PASS, result.summary
    root = (ctx.build_dir / BUILD_SUBDIR).resolve()
    for mode, argv in result.details["argv"].items():
        directory = root / MODE_DIRS[mode]
        assert _units(argv) == [str(directory / "src/impl.i"), str(directory / PREPROCESSED_HARNESS)], mode
        assert "src/impl.c" not in argv and "harness/h.c" not in argv
    vacuity_harness = (root / "vacuity" / PREPROCESSED_HARNESS).read_text(encoding="utf-8")
    assert '__CPROVER_assert(0, "leanc_reach:" "after_call")' in vacuity_harness
    assert "leanc_reach" not in (root / "verify" / PREPROCESSED_HARNESS).read_text(encoding="utf-8")


def test_a_harness_armed_by_the_raw_verify_output_sees_the_state_before_the_verify_run(tmp_path):
    ctx = _context(tmp_path, "count_zeros_raw_output_harness")
    result = run_cbmc(ctx)
    raw_output = (ctx.snapshot_dir / "harness" / RAW_VERIFY_OUTPUT).resolve()
    assert raw_output == (ctx.raw_dir / "cbmc-verify.json").resolve() and raw_output.is_file()
    assert result.outcome is Outcome.INCONCLUSIVE and result.status is Status.UNKNOWN, result.summary
    assert result.details["vacuity"]["probes"] == {"after_call": False}
    assert "if (0)" in (ctx.build_dir / BUILD_SUBDIR / "vacuity" / PREPROCESSED_HARNESS).read_text(encoding="utf-8")


def test_the_build_clock_is_pinned_in_every_preprocessed_text(tmp_path):
    ctx = _context(tmp_path, "count_zeros_build_time_harness")
    result = run_cbmc(ctx)
    for mode in ("verify", "vacuity"):
        text = (ctx.build_dir / BUILD_SUBDIR / mode / PREPROCESSED_HARNESS).read_text(encoding="utf-8")
        assert PINNED_DATE_TIME in text, mode
    assert result.outcome is Outcome.INCONCLUSIVE, result.summary
    assert result.details["vacuity"]["probes"] == {"after_call": False}


@pytest.mark.parametrize("form", SHADOW_FORMS)
def test_a_harness_that_declares_a_cprover_name_is_an_error(tmp_path, form):
    result = run_cbmc(_context(tmp_path, f"count_zeros_shadow_{form}_harness"))
    assert result.outcome is Outcome.ERROR and result.status is Status.TOOL_ERROR, result.summary
    assert CPROVER_DECLARATION_RULE in result.summary and "__CPROVER_assert" in result.summary
    assert "vacuity" not in result.details["argv"]
    assert {diagnostic.rule_id for diagnostic in result.diagnostics} == {"cprover-declaration"}


def test_a_probe_site_that_cbmc_does_not_report_is_a_vacuous_harness(tmp_path):
    result = run_cbmc(_context(tmp_path, "count_zeros_dead_probe_harness"))
    assert result.outcome is Outcome.INCONCLUSIVE and result.status is Status.UNKNOWN, result.summary
    vacuity = result.details["vacuity"]
    assert vacuity["status"] == "vacuous" and vacuity["sites"] == 2
    assert vacuity["unreported_sites"] == ['LEANC_REACH("dead") at harness/h.c:13']
    (failure,) = result.failures
    assert failure.id == f"cbmc:vacuity:dead{SITE_ID_INFIX}harness/h.c:13" and failure.kind == "vacuity"
    assert "1 LEANC_REACH site(s) of the vacuity program not reported by CBMC" in result.summary


def test_the_control_harness_reports_every_site(tmp_path):
    result = run_cbmc(_context(tmp_path, "count_zeros_harness"))
    assert result.outcome is Outcome.PASS, result.summary
    assert result.details["vacuity"]["sites"] == 1 and result.details["vacuity"]["unreported_sites"] == []


def _appended(tmp_path: Path, rel: str, tail: bytes) -> BackendContext:
    ctx = _context(tmp_path, "count_zeros_harness")
    path = ctx.snapshot_dir / rel
    path.write_bytes(path.read_bytes() + tail)
    return ctx


@pytest.mark.parametrize(
    "rel, tail, rule",
    [
        (EDITABLE, MISSING_TAIL_INCLUDE, SILENT_PREPROCESSING_FAILURE),
        (HARNESS, MISSING_TAIL_INCLUDE, SILENT_PREPROCESSING_FAILURE),
        (EDITABLE, ERROR_DIRECTIVE, SILENT_PREPROCESSING_FAILURE),
        (EDITABLE, LATIN1_LITERAL, NOT_UTF8_OUTPUT),
    ],
)
def test_a_source_that_cbmc_preprocesses_with_exit_code_zero_but_not_faithfully_is_an_error(tmp_path, rel, tail, rule):
    result = run_cbmc(_appended(tmp_path, rel, tail))
    assert result.outcome is Outcome.ERROR and result.status is Status.TOOL_ERROR, result.summary
    assert f"cbmc --preprocess of {rel} {rule}" in result.summary
    assert "verify" not in result.details["argv"]


def test_the_silent_failure_names_the_preprocessor_error(tmp_path):
    result = run_cbmc(_appended(tmp_path, EDITABLE, MISSING_TAIL_INCLUDE))
    assert f"fatal error: {ABSENT_HEADER}" in result.summary


def test_a_project_header_cannot_reach_the_library_models_of_the_preprocessed_runs(tmp_path):
    ctx = make_cbmc_project(tmp_path, src="add", harness="add_harness", function="add")
    (ctx.snapshot_dir / "include" / "string.h").write_text(SHADOWING_STRING_H)
    (ctx.snapshot_dir / EDITABLE).write_text(OVERLAPPING_COPY)
    result = run_cbmc(ctx)
    assert result.outcome is Outcome.FAIL, result.summary
    assert any("precondition" in failure.id for failure in result.failures), [failure.id for failure in result.failures]
