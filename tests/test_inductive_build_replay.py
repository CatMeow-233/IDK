from __future__ import annotations

import json
import signal
from pathlib import Path
from typing import Any

import pytest
from cprover_support import measured_toolchain, release_line, with_tool
from inductive_build_support import (
    CONTRACTS,
    INDUCTIVE_HARNESS,
    TALLY,
    TALLY_FUNCTIONS,
    TALLY_SOURCE,
    no_contracts,
    tally_context,
    tally_spec_data,
)
from inductive_replay_support import GOTO_CC, NO_OUTPUT_FILE, OUTPUT_FLAG, START_ERROR, load_recording, mutated, replay_build

from leanc.backends.cbmc.compile_flags import cbmc_preprocessor_macro_args
from leanc.backends.cbmc.preprocess import CBMC_BANNER_PREFIX
from leanc.backends.cbmc.vacuity_divergence import DIVERGENCE_RULE
from leanc.backends.inductive import BuildMode, build_inductive
from leanc.backends.inductive.harness_calls import return_probe_label
from leanc.backends.inductive.link import GOTO_CC_VERBOSITY
from leanc.backends.inductive.loops import DFCC_ARGS
from leanc.backends.inductive.program import TRUSTED_HARNESS_STEM, TRUSTED_MODELS_STEM
from leanc.foundation.reasons import Reason
from leanc.foundation.status import Outcome
from leanc.rules.loop_sites import function_probe_label, loop_probe_label
from leanc.toolchain.context import BackendContext

TOOL_RUNS = 20
CONTRACTED_LINES = (6, 18, 29)
HARNESS_PREPROCESS = "preprocess harness harness/inductive.c"
HARNESS_VACUITY_PREPROCESS = "preprocess harness vacuity harness/inductive.c"
EDITABLE_PREPROCESS = f"preprocess editable {TALLY_SOURCE}"
UNDECLARED_WARNING = "harness/inductive.c:12:1: warning: function 'nondet_ulonglong' is not declared"
EXTRA_LOOP = {"name": "tally_clear.0", "sourceLocation": {"file": TALLY_SOURCE, "function": "tally_clear", "line": "33"}}


@pytest.fixture
def recording() -> list[dict[str, Any]]:
    return load_recording()


@pytest.fixture
def ctx(tmp_path: Path) -> BackendContext:
    context = tally_context(tmp_path)
    context.tools = measured_toolchain()
    return context


def set_fields(**fields: Any):
    def change(entry: dict[str, Any]) -> None:
        entry.update(fields)

    return change


def edit_stdout(transform):
    def change(entry: dict[str, Any]) -> None:
        entry["stdout"] = transform(entry["stdout"])

    return change


def symbol_table_edit(transform):
    def change(entry: dict[str, Any]) -> None:
        messages = json.loads(entry["stdout"])
        table = next(message["symbolTable"] for message in messages if "symbolTable" in message)
        transform(table)
        entry["stdout"] = json.dumps(messages)

    return change


def loops_edit(transform):
    def change(entry: dict[str, Any]) -> None:
        messages = json.loads(entry["stdout"])
        transform(next(message["loops"] for message in messages if "loops" in message))
        entry["stdout"] = json.dumps(messages)

    return change


def replayed(ctx: BackendContext, entries: list[dict[str, Any]]):
    build, replayer = replay_build(ctx, entries)
    return build, replayer


def test_recorded_build_succeeds(ctx, recording):
    build, replayer = replayed(ctx, recording)
    assert build.problem is None and build.built
    assert len(replayer.calls) == TOOL_RUNS == len(build.invocations)
    assert [item.contract.line for item in build.contracts] == list(CONTRACTED_LINES)
    assert [call.function for call in build.claimed_calls] == list(TALLY_FUNCTIONS)
    assert build.function_probes == tuple(function_probe_label(name) for name in TALLY_FUNCTIONS)
    assert build.loop_probes == tuple(loop_probe_label(TALLY_SOURCE, line) for line in CONTRACTED_LINES)
    assert build.return_probes == tuple(return_probe_label(name) for name in TALLY_FUNCTIONS)
    assert build.returns["tally_clear"].value == "void" and build.returns["tally_below"].value == "value"
    assert build.verify.mode is BuildMode.VERIFY and build.verify.instrumented.name == "dfcc.goto"
    assert build.vacuity.mode is BuildMode.VACUITY
    assert "compile_flags" not in build.details()


def test_goto_cc_links_trusted_units_first_with_warnings_enabled(ctx, recording):
    build, _ = replayed(ctx, recording)
    link = build.argv["verify link"]
    assert link[1:3] == list(GOTO_CC_VERBOSITY)
    units = [Path(arg).stem for arg in link if arg.endswith(".i")]
    assert units == [TRUSTED_MODELS_STEM, TRUSTED_HARNESS_STEM, "editable_00"]


def test_dfcc_runs_with_main_and_loop_contracts(ctx, recording):
    build, _ = replayed(ctx, recording)
    for mode in BuildMode:
        argv = build.argv[f"{mode.value} dfcc"]
        assert argv[1:4] == list(DFCC_ARGS)
        assert Path(argv[-1]).name == "dfcc.goto"


def test_vacuity_harness_is_preprocessed_with_the_vacuity_define(ctx, recording):
    build, _ = replayed(ctx, recording)
    assert "-DLEANC_VACUITY_CHECK" in build.argv["preprocess harness vacuity harness/inductive.c"]
    assert "-DLEANC_VACUITY_CHECK" not in build.argv[HARNESS_PREPROCESS]


MACRO_FLAGS = ["-funsigned-char", "-std=gnu99", "-DW=3"]
GOTO_CC_RUNS = 7


def contains_slice(argv: list[str], part: list[str]) -> bool:
    return any(argv[start : start + len(part)] == part for start in range(len(argv) - len(part) + 1))


def test_every_goto_cc_run_gets_the_cbmc_macro_args_before_its_output(tmp_path, recording):
    data = tally_spec_data()
    data["compile"] = {"flags": list(MACRO_FLAGS)}
    context = tally_context(tmp_path, data=data)
    context.tools = measured_toolchain()
    build, _ = replayed(context, recording)
    assert build.built, build.problem
    expected = cbmc_preprocessor_macro_args(context.spec.compile, context.spec.language.standard)
    runs = [argv for argv in build.argv.values() if Path(argv[0]).name == GOTO_CC]
    assert len(runs) == GOTO_CC_RUNS
    for argv in runs:
        before_output = argv[: argv.index(OUTPUT_FLAG)]
        assert contains_slice(before_output, expected), argv
        assert set(MACRO_FLAGS) <= set(before_output), argv


def test_object_bits_reach_goto_cc(tmp_path, recording):
    context = tally_context(tmp_path, data=tally_spec_data(object_bits=16))
    context.tools = measured_toolchain()
    build, _ = replayed(context, recording)
    link = build.argv["verify link"]
    assert link[link.index("--object-bits") + 1] == "16"


@pytest.mark.parametrize(
    "tool, version",
    [("goto-cc", None), ("goto-instrument", release_line("goto-instrument", "5.94.0")), ("cbmc", "garbage")],
)
def test_tool_pin_failure_is_a_tool_error_before_any_run(ctx, tool, version):
    path = None if version is None else "/opt/cprover/bin/" + tool
    ctx.tools = with_tool(measured_toolchain(), tool, path=path, version=version)
    build = build_inductive(ctx)
    assert build.problem.outcome is Outcome.ERROR and build.problem.reason is Reason.TOOL_ERROR
    assert build.invocations == []


def test_missing_harness_is_a_tool_error(ctx):
    (ctx.snapshot_dir / INDUCTIVE_HARNESS).unlink()
    build = build_inductive(ctx)
    assert build.problem.outcome is Outcome.ERROR and INDUCTIVE_HARNESS in build.problem.describe()


def test_malloc_may_fail_is_rejected(tmp_path):
    context = tally_context(tmp_path, data=tally_spec_data(malloc_may_fail=True))
    context.tools = measured_toolchain()
    build = build_inductive(context)
    assert build.problem.outcome is Outcome.ERROR and "malloc_may_fail" in build.problem.summary


def test_spec_without_inductive_section_is_a_tool_error(tmp_path):
    data = tally_spec_data()
    del data["inductive"]
    context = tally_context(tmp_path, data=data)
    assert build_inductive(context).problem.outcome is Outcome.ERROR


def test_spec_without_verification_section_is_a_tool_error(tmp_path):
    data = tally_spec_data()
    del data["verification"]
    data["gates"] = ["compile"]
    context = tally_context(tmp_path, data=data)
    context.tools = measured_toolchain()
    build = build_inductive(context)
    assert build.problem.outcome is Outcome.ERROR and "verification section" in build.problem.summary


def test_dfcc_that_writes_no_program_is_a_tool_error(ctx, recording):
    build, _ = replayed(ctx, mutated(recording, "verify dfcc", set_fields(**{NO_OUTPUT_FILE: True})))
    assert build.problem.outcome is Outcome.ERROR and "wrote no instrumented program" in build.problem.describe()


def test_stale_dfcc_program_from_an_earlier_build_is_not_used(ctx, recording):
    stale = Path(ctx.build_dir) / "inductive" / "verify" / "dfcc.goto"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"stale")
    build, _ = replayed(ctx, mutated(recording, "verify dfcc", set_fields(**{NO_OUTPUT_FILE: True})))
    assert build.problem.outcome is Outcome.ERROR and "wrote no instrumented program" in build.problem.describe()


def test_line_splice_in_preprocessed_output_is_a_tool_error(ctx, recording):
    def splice(text: str) -> str:
        return text.replace("int c = 0;", "int c = \\\n0;")

    build, _ = replayed(ctx, mutated(recording, EDITABLE_PREPROCESS, edit_stdout(splice)))
    assert build.problem.outcome is Outcome.ERROR and "line splice" in build.problem.describe()


def test_claimed_function_without_editable_definition_is_a_tool_error(tmp_path, recording):
    context = tally_context(tmp_path, data=tally_spec_data((*TALLY_FUNCTIONS, "tally_extra")))
    context.tools = measured_toolchain()

    def call_extra(text: str) -> str:
        return text.replace("tally_clear(a, n);", "tally_clear(a, n); tally_extra(a, n);")

    entries = mutated(recording, HARNESS_PREPROCESS, edit_stdout(call_extra))
    entries = mutated(entries, HARNESS_VACUITY_PREPROCESS, edit_stdout(call_extra))
    build, replayer = replayed(context, entries)
    assert build.problem.outcome is Outcome.ERROR
    assert "tally_extra must have exactly one definition in the editable sources, found 0" in build.problem.describe()
    assert len(replayer.calls) == 4


def test_claimed_function_defined_twice_is_a_tool_error(ctx, recording):
    def second_definition(text: str) -> str:
        return text + "\nstatic int tally_max(const int *a, int n) { return n; }\n"

    build, _ = replayed(ctx, mutated(recording, EDITABLE_PREPROCESS, edit_stdout(second_definition)))
    assert build.problem.outcome is Outcome.ERROR and "found 2" in build.problem.describe()


def test_schema_breaking_contracts_are_rejected_before_any_run(tmp_path):
    context = tally_context(tmp_path, contracts='{"loops": [{"file": "src/tally.c"}]}')
    context.tools = measured_toolchain()
    build = build_inductive(context)
    assert build.problem.outcome is Outcome.INCONCLUSIVE and build.problem.reason is Reason.CONTRACT_REJECTED
    assert build.invocations == []


def test_macro_named_clause_is_rejected(tmp_path):
    source = (TALLY / TALLY_SOURCE).read_text(encoding="utf-8").replace('#include "tally.h"', '#include "tally.h"\n#define LIMIT n')
    contracts = (TALLY / "loops.json").read_text(encoding="utf-8").replace('"i <= n", "0 <= c"', '"i <= LIMIT", "0 <= c"')
    context = tally_context(tmp_path, contracts=contracts, extra={TALLY_SOURCE: source})
    context.tools = measured_toolchain()
    build = build_inductive(context)
    assert build.problem.reason is Reason.CONTRACT_REJECTED and "LIMIT" in build.problem.describe()


@pytest.mark.parametrize(
    "label, change, outcome, needle",
    [
        (EDITABLE_PREPROCESS, set_fields(exit_code=1, stderr="tally.c:1: error: boom"), Outcome.ERROR, "exit code 1"),
        (EDITABLE_PREPROCESS, set_fields(timed_out=True, exit_code=None), Outcome.TIMEOUT, "timed out"),
        (EDITABLE_PREPROCESS, set_fields(exit_code=-9, signal=signal.SIGKILL), Outcome.ERROR, "SIGKILL"),
        (EDITABLE_PREPROCESS, set_fields(exit_code=139), Outcome.ERROR, "SIGSEGV"),
        (EDITABLE_PREPROCESS, set_fields(output_truncated=True), Outcome.ERROR, "truncated"),
        (EDITABLE_PREPROCESS, edit_stdout(lambda text: text.partition("\n")[2]), Outcome.ERROR, CBMC_BANNER_PREFIX),
        (EDITABLE_PREPROCESS, edit_stdout(lambda text: text.replace('"src/tally.c"', '"src/other.c"')), Outcome.ERROR, "no linemarker"),
        ("preprocess models inductive_models.c", set_fields(exit_code=2), Outcome.ERROR, "exit code 2"),
        ("verify compile trusted_1_harness", set_fields(exit_code=1, stderr="error: x"), Outcome.ERROR, "trusted unit"),
        ("verify symbols trusted_0_models", set_fields(stdout="not json"), Outcome.ERROR, "not valid JSON"),
        ("verify link", set_fields(exit_code=1, stderr="error: duplicate"), Outcome.ERROR, "linked with the editable"),
        ("vacuity compile editable", set_fields(exit_code=1, stderr="error: probe"), Outcome.ERROR, "vacuity build failed"),
        ("vacuity compile editable", set_fields(timed_out=True, exit_code=None), Outcome.TIMEOUT, "timed out"),
        ("verify link", set_fields(timed_out=True, exit_code=None), Outcome.TIMEOUT, "timed out"),
        ("verify dfcc", set_fields(**{START_ERROR: "no such file", "exit_code": None}), Outcome.ERROR, "could not be started"),
        ("verify dfcc", set_fields(exit_code=6, stderr="dfcc failure"), Outcome.ERROR, "exit code 6"),
        ("vacuity dfcc", set_fields(exit_code=134), Outcome.ERROR, "SIGABRT"),
        ("verify loops", set_fields(stdout="[]"), Outcome.ERROR, "expected one loops list"),
        ("vacuity loops", set_fields(timed_out=True, exit_code=None), Outcome.TIMEOUT, "verification.timeout_s=120"),
    ],
)
def test_failing_steps_never_build(ctx, recording, label, change, outcome, needle):
    build, _ = replayed(ctx, mutated(recording, label, change))
    assert not build.built
    assert build.problem.outcome is outcome
    assert needle in build.problem.describe()


def test_editable_compile_failure_with_clauses_rejects_the_contracts(ctx, recording):
    change = set_fields(exit_code=1, stderr="src/tally.c:6:1: error: failed to find symbol 'missing'")
    build, _ = replayed(ctx, mutated(recording, "verify compile editable", change))
    assert build.problem.reason is Reason.CONTRACT_REJECTED
    assert "failed to find symbol 'missing'" in build.problem.describe()


def test_editable_compile_failure_without_clauses_is_a_tool_error(tmp_path):
    context = tally_context(tmp_path, contracts=no_contracts())
    context.tools = measured_toolchain()
    recording = [entry for entry in load_recording()]
    change = set_fields(exit_code=1, stderr="src/tally.c:3:1: error: x")
    build, _ = replayed(context, mutated(recording, "verify compile editable", change))
    assert build.problem.outcome is Outcome.ERROR and build.problem.reason is Reason.TOOL_ERROR


def test_contract_on_a_line_without_loop_is_rejected(tmp_path, recording):
    contracts = (TALLY / "loops.json").read_text(encoding="utf-8").replace('"line": 6', '"line": 7')
    context = tally_context(tmp_path, contracts=contracts)
    context.tools = measured_toolchain()
    build, replayer = replayed(context, recording)
    assert build.problem.reason is Reason.CONTRACT_REJECTED and "src/tally.c:7" in build.problem.describe()
    assert len(replayer.calls) == 4


def test_link_rename_of_a_trusted_symbol_is_a_tool_error(ctx, recording):
    def add_rename(table: dict) -> None:
        table["main$link1"] = {**table["main"], "baseName": "main"}

    build, _ = replayed(ctx, mutated(recording, "verify symbols linked", symbol_table_edit(add_rename)))
    assert build.problem.outcome is Outcome.ERROR and "main$link1" in build.problem.describe()


def test_editable_reserved_name_is_a_tool_error(ctx, recording):
    def add_hook(table: dict) -> None:
        table["leanc_hook"] = {**table["tally_below"], "baseName": "leanc_hook"}

    build, _ = replayed(ctx, mutated(recording, "vacuity symbols linked", symbol_table_edit(add_hook)))
    assert build.problem.outcome is Outcome.ERROR and "leanc_hook" in build.problem.describe()


def test_claimed_function_outside_the_editable_sources_is_a_tool_error(ctx, recording):
    def move(table: dict) -> None:
        table["tally_max"]["location"]["namedSub"]["file"]["id"] = INDUCTIVE_HARNESS

    build, _ = replayed(ctx, mutated(recording, "verify symbols linked", symbol_table_edit(move)))
    assert build.problem.outcome is Outcome.ERROR and "outside the editable sources" in build.problem.describe()


def test_unreadable_return_type_is_a_tool_error(ctx, recording):
    def drop(table: dict) -> None:
        del table["tally_clear"]["type"]["namedSub"]

    build, _ = replayed(ctx, mutated(recording, "verify symbols linked", symbol_table_edit(drop)))
    assert build.problem.outcome is Outcome.ERROR and "return type of claimed function tally_clear" in build.problem.describe()


def test_undeclared_function_warning_is_inconclusive(ctx, recording):
    build, _ = replayed(ctx, mutated(recording, "verify compile trusted_1_harness", set_fields(stderr=UNDECLARED_WARNING)))
    assert build.problem.outcome is Outcome.INCONCLUSIVE and build.problem.reason is Reason.UNDECLARED_FUNCTION
    assert "harness/inductive.c:12: nondet_ulonglong" in build.problem.describe()


def test_undeclared_function_warning_in_the_vacuity_link_is_inconclusive(ctx, recording):
    build, _ = replayed(ctx, mutated(recording, "vacuity link", set_fields(stdout=UNDECLARED_WARNING)))
    assert build.problem.reason is Reason.UNDECLARED_FUNCTION


@pytest.mark.parametrize("label", ["verify loops", "vacuity loops"])
def test_residual_loop_stops_the_build(ctx, recording, label):
    build, replayer = replayed(ctx, mutated(recording, label, loops_edit(lambda loops: loops.append(EXTRA_LOOP))))
    assert build.problem.outcome is Outcome.INCONCLUSIVE and build.problem.reason is Reason.LOOP_WITHOUT_CONTRACT
    assert "src/tally.c:33 (CBMC loop tally_clear.0)" in build.problem.details
    assert not build.built


def test_unmodelled_bodiless_functions_after_dfcc_are_recorded(ctx, recording):
    warning = "file src/tally.c line 3: no body for function 'mystery'\nno body for function '__CPROVER_assignable'"
    build, _ = replayed(ctx, mutated(recording, "verify dfcc", set_fields(stderr=warning)))
    assert build.built and build.bodiless == ("mystery",)


def test_harness_rule_violation_is_a_tool_error_before_compiling(ctx, recording):
    def second_main(text: str) -> str:
        return text + "\nint main2(void) { return main(); }\n"

    entries = mutated(recording, HARNESS_PREPROCESS, edit_stdout(second_main))
    build, replayer = replayed(ctx, mutated(entries, HARNESS_VACUITY_PREPROCESS, edit_stdout(second_main)))
    assert build.problem.outcome is Outcome.ERROR and "main is referenced" in build.problem.describe()
    assert len(replayer.calls) == 4


@pytest.mark.parametrize("label", [HARNESS_PREPROCESS, HARNESS_VACUITY_PREPROCESS])
def test_harness_text_that_differs_between_the_builds_is_a_tool_error_before_compiling(ctx, recording, label):
    def extra_statement(text: str) -> str:
        return text + "\nint leanc_free_extra;\n"

    build, replayer = replayed(ctx, mutated(recording, label, edit_stdout(extra_statement)))
    assert build.problem.outcome is Outcome.ERROR, build.problem
    assert DIVERGENCE_RULE in build.problem.describe()
    assert len(replayer.calls) == 4


def test_harness_without_reach_probe_is_a_tool_error_before_compiling(ctx, recording):
    harness = ctx.snapshot_dir / INDUCTIVE_HARNESS
    harness.write_text(harness.read_text(encoding="utf-8").replace("LEANC_REACH(", "(void)("), encoding="utf-8")
    build, replayer = replayed(ctx, recording)
    assert build.problem.outcome is Outcome.ERROR and "no LEANC_REACH probe" in build.problem.describe()
    assert len(replayer.calls) == 4


def test_vacuity_build_with_different_claimed_calls_is_a_tool_error(ctx, recording):
    def extra_call(text: str) -> str:
        return text.replace("tally_clear(a, n);", "tally_clear(a, n); tally_clear(a, n);")

    build, _ = replayed(ctx, mutated(recording, HARNESS_VACUITY_PREPROCESS, edit_stdout(extra_call)))
    assert build.problem.outcome is Outcome.ERROR and DIVERGENCE_RULE in build.problem.describe()
    assert "tally_clear(a, n); tally_clear(a, n);" in build.problem.describe()


def test_editable_code_must_not_reference_main(ctx, recording):
    def call_main(text: str) -> str:
        return text.replace("return c;", "return c + main();")

    build, _ = replayed(ctx, mutated(recording, EDITABLE_PREPROCESS, edit_stdout(call_main)))
    assert build.problem.outcome is Outcome.ERROR and "editable code references main" in build.problem.describe()


def test_missing_contracts_file_means_no_contracts(tmp_path, recording):
    context = tally_context(tmp_path)
    (context.snapshot_dir / CONTRACTS).unlink()
    context.tools = measured_toolchain()
    build, _ = replayed(context, recording)
    assert build.contracts == () and build.loop_probes == ()
    written = build.verify.linked.parent / "editable_00.i"
    assert "__CPROVER_assigns" not in written.read_text(encoding="utf-8")


def test_written_units_carry_clauses_and_probes(ctx, recording):
    build, _ = replayed(ctx, recording)
    verify_unit = (build.verify.linked.parent / "editable_00.i").read_text(encoding="utf-8")
    vacuity_unit = (build.vacuity.linked.parent / "editable_00.i").read_text(encoding="utf-8")
    assert verify_unit.count("__CPROVER_assigns(") == len(CONTRACTED_LINES)
    assert "leanc_loop:" not in verify_unit and "leanc_function:" not in verify_unit
    assert vacuity_unit.count("__CPROVER_assigns(") == len(CONTRACTED_LINES)
    for label in (*build.loop_probes, *build.function_probes):
        assert f'"{label}"' in vacuity_unit
    vacuity_harness = (build.vacuity.linked.parent / f"{TRUSTED_HARNESS_STEM}.i").read_text(encoding="utf-8")
    for label in build.return_probes:
        assert f'"{label}"' in vacuity_harness
    verify_harness = (build.verify.linked.parent / f"{TRUSTED_HARNESS_STEM}.i").read_text(encoding="utf-8")
    assert "leanc_return:" not in verify_harness
