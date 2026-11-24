from __future__ import annotations

import json
from pathlib import Path

import pytest
from goto_pipeline_support import run_tool, tool
from inductive_build_support import (
    TALLY,
    TALLY_FUNCTIONS,
    built,
    capture_context,
    cbmc_properties,
    holds,
    no_contracts,
    reached,
    tally_context,
    tally_spec_data,
)
from inductive_replay_support import load_recording, record_build

from leanc.backends.cbmc.symbol_table import parse_symbol_table
from leanc.backends.inductive import ASSUME_AFTER_CLAIMED_CALL_LABEL, BuildMode
from leanc.backends.inductive.harness_calls import claimed_call_label
from leanc.backends.inductive.loops import parse_loops
from leanc.backends.inductive.program import TRUSTED_HARNESS_STEM, TRUSTED_MODELS_STEM
from leanc.backends.inductive.symbols import capture_problems, trusted_definitions
from leanc.foundation.reasons import Reason
from leanc.foundation.status import Outcome
from leanc.toolchain.context import VerifyOptions

pytestmark = pytest.mark.usefixtures("needs_cbmc")

BELOW = ("tally_below",)
TALLY_SOURCE = "src/tally.c"
SPLIT_SOURCE = "src/clear.c"
SPLIT_LOOP_LINE = 5
SYMBOL_STEP = "symbols"
LOOP_STEP = "loops"


def tally_build(tmp_path, harness="branches.c", functions=TALLY_FUNCTIONS, **project):
    return built(tally_context(tmp_path, harness=harness, data=tally_spec_data(functions), **project))


def test_tally_build_gives_programs_whose_instrumentation_holds_and_whose_probes_are_reached(tmp_path):
    build = tally_build(tmp_path)
    assert build.built, build.problem
    verify = cbmc_properties(build.verify.instrumented, checks=True)
    failing = [description for description, statuses in verify.items() if not holds(verify, description)]
    assert failing == []
    assert all(holds(verify, claimed_call_label(name)) for name in TALLY_FUNCTIONS)
    vacuity = cbmc_properties(build.vacuity.instrumented, checks=False)
    for label in (*build.function_probes, *build.loop_probes, *build.return_probes):
        assert reached(vacuity, label), label


@pytest.mark.parametrize(
    "harness, functions, second_calls",
    [
        ("sequential.c", TALLY_FUNCTIONS, ("tally_below", "tally_max")),
        ("nested.c", ("tally_below", "tally_max"), ("tally_max",)),
        ("helper_twice.c", BELOW, BELOW),
        ("loop_calls.c", ("tally_clear",), ("tally_clear",)),
    ],
)
def test_a_second_claimed_call_on_one_path_fails_its_check(tmp_path, harness, functions, second_calls):
    build = tally_build(tmp_path, harness, functions)
    assert build.built, build.problem
    verify = cbmc_properties(build.verify.instrumented, checks=False)
    for name in functions:
        assert reached(verify, claimed_call_label(name)) == (name in second_calls), name


@pytest.mark.parametrize(
    "harness", ["assume_in_helper.c", "assume_condition_call.c", "unsat_condition.c", "unsat_helper.c"]
)
def test_assumption_after_a_claimed_call_through_a_helper_fails_its_check(tmp_path, harness):
    build = tally_build(tmp_path, harness, BELOW)
    assert build.built, build.problem
    verify = cbmc_properties(build.verify.instrumented, checks=False)
    assert reached(verify, ASSUME_AFTER_CLAIMED_CALL_LABEL)


def test_assumptions_before_the_claimed_call_pass_their_check(tmp_path):
    build = tally_build(tmp_path, "helper_twice.c", BELOW)
    verify = cbmc_properties(build.verify.instrumented, checks=False)
    assert holds(verify, ASSUME_AFTER_CLAIMED_CALL_LABEL)


@pytest.mark.parametrize(
    "harness, functions, needle",
    [
        ("assume_after.c", BELOW, "LEANC_ASSUME after the call of the claimed function tally_below"),
        ("pointer.c", BELOW, "used other than in a direct call"),
        ("main_call.c", BELOW, "main is referenced inside a function body"),
        ("no_reach.c", TALLY_FUNCTIONS, "no LEANC_REACH probe"),
    ],
)
def test_harness_rule_violations_are_errors(tmp_path, harness, functions, needle):
    build = tally_build(tmp_path, harness, functions)
    assert build.problem.outcome is Outcome.ERROR
    assert needle in build.problem.describe()


def test_undeclared_harness_input_is_inconclusive(tmp_path):
    build = tally_build(tmp_path, "undeclared.c", BELOW)
    assert build.problem.outcome is Outcome.INCONCLUSIVE and build.problem.reason is Reason.UNDECLARED_FUNCTION
    assert "nondet_ulonglong" in build.problem.describe()


def test_undeclared_call_in_editable_code_is_inconclusive(tmp_path):
    build = tally_build(tmp_path, source=TALLY / "src" / "undeclared.c")
    assert build.problem.reason is Reason.UNDECLARED_FUNCTION and "helper_without_prototype" in build.problem.describe()


def test_editable_static_named_like_a_trusted_function_is_caught_with_trusted_units_first(tmp_path):
    build = built(capture_context(tmp_path))
    assert build.problem.outcome is Outcome.ERROR
    assert "helper$link1" in build.problem.describe()


def test_editable_static_named_like_a_trusted_function_is_caught_with_editable_units_first(tmp_path):
    build = built(capture_context(tmp_path))
    verify_dir = build.argv["verify link"]
    units = [arg for arg in verify_dir if arg.endswith(".i")]
    trusted_units, editable_units = units[:2], units[2:]
    output = str(tmp_path / "editable_first.goto")
    linked = run_tool([tool("goto-cc"), "-o", output, *editable_units, *trusted_units], tmp_path / "proj")
    assert linked.returncode == 0, linked.stderr
    table = parse_symbol_table(run_tool([tool("goto-instrument"), "--show-symbol-table", "--json-ui", output], tmp_path).stdout)
    definitions = []
    for unit in trusted_units:
        alone = str(tmp_path / "alone.goto")
        assert run_tool([tool("goto-cc"), "-o", alone, unit], tmp_path / "proj").returncode == 0
        single = parse_symbol_table(run_tool([tool("goto-instrument"), "--show-symbol-table", "--json-ui", alone], tmp_path).stdout)
        definitions.append(trusted_definitions(single, unit.rsplit("/", 1)[-1].removesuffix(".i")))
    problems = capture_problems(table, definitions, ["src/ed.c"])
    assert any("trusted symbol helper defined at harness/h.c" in problem for problem in problems)
    assert any("helper$link" in problem for problem in problems)


A15 = Path(__file__).resolve().parent / "fixtures" / "inductive" / "attack"
A15_TRUSTED = ("a15_m.c", "a15_h.c")
A15_EDITABLE = "a15_ed.c"


def _preprocessed_unit(tmp_path, name: str, stem: str) -> str:
    done = run_tool([tool("cbmc"), "--preprocess", name], A15)
    assert done.returncode == 0, done.stderr
    unit = tmp_path / f"{stem}.i"
    unit.write_text(done.stdout.partition("\n")[2], encoding="utf-8")
    return str(unit)


def _table(tmp_path, units: list[str], name: str):
    output = str(tmp_path / name)
    done = run_tool([tool("goto-cc"), "--verbosity", "2", "-o", output, *units], tmp_path)
    assert done.returncode == 0, done.stderr
    return parse_symbol_table(run_tool([tool("goto-instrument"), "--show-symbol-table", "--json-ui", output], tmp_path).stdout)


@pytest.mark.parametrize("trusted_first", [True, False])
def test_measured_a15_capture_is_caught_in_both_link_orders(tmp_path, trusted_first):
    trusted = [_preprocessed_unit(tmp_path, name, f"trusted_{index}") for index, name in enumerate(A15_TRUSTED)]
    editable = _preprocessed_unit(tmp_path, A15_EDITABLE, "editable_00")
    definitions = [
        trusted_definitions(_table(tmp_path, [unit], f"alone_{index}.goto"), Path(unit).stem)
        for index, unit in enumerate(trusted)
    ]
    order = [*trusted, editable] if trusted_first else [editable, *trusted]
    problems = capture_problems(_table(tmp_path, order, "linked.goto"), definitions, [A15_EDITABLE])
    assert any("helper$link1" in problem for problem in problems)
    if not trusted_first:
        assert any(f"trusted symbol helper defined at {A15_TRUSTED[1]}" in problem for problem in problems)
        assert any("helper$link2" in problem for problem in problems)


@pytest.mark.parametrize("source, name", [("hooked.c", "leanc_hook"), ("hooked_macro.c", "leanc_hidden")])
def test_editable_leanc_definitions_are_caught_on_the_expanded_view(tmp_path, source, name):
    build = tally_build(tmp_path, source=TALLY / "src" / source, contracts=no_contracts())
    assert build.problem.outcome is Outcome.ERROR
    assert f"{name} uses the leanc_ prefix reserved" in build.problem.describe()


def test_uncontracted_loops_stop_the_build_before_cbmc(tmp_path):
    build = tally_build(tmp_path, contracts=no_contracts())
    assert build.problem.outcome is Outcome.INCONCLUSIVE and build.problem.reason is Reason.LOOP_WITHOUT_CONTRACT
    assert sorted(build.problem.details) == [
        "src/tally.c:18 (CBMC loop tally_max.0)",
        "src/tally.c:29 (CBMC loop tally_clear.0)",
        "src/tally.c:6 (CBMC loop tally_below.0)",
    ]
    assert build.verify is None and build.vacuity is None


def test_do_while_loop_stays_residual(tmp_path):
    contracts = json.dumps({"loops": json.loads((TALLY / "loops.json").read_text(encoding="utf-8"))["loops"][:2]})
    build = tally_build(tmp_path, source=TALLY / "src" / "do_while.c", contracts=contracts)
    assert build.problem.reason is Reason.LOOP_WITHOUT_CONTRACT
    assert build.problem.details == ("src/tally.c:33 (CBMC loop tally_clear.0)",)


def test_clause_that_does_not_compile_rejects_the_contracts(tmp_path):
    contracts = (TALLY / "loops.json").read_text(encoding="utf-8").replace('"0 <= c"', '"0 <= missing_name"')
    build = tally_build(tmp_path, contracts=contracts)
    assert build.problem.outcome is Outcome.INCONCLUSIVE and build.problem.reason is Reason.CONTRACT_REJECTED
    assert "missing_name" in build.problem.describe()


def test_two_editable_files_are_annotated_separately(tmp_path):
    entries = json.loads((TALLY / "loops.json").read_text(encoding="utf-8"))["loops"]
    entries[2] = {**entries[2], "file": SPLIT_SOURCE, "line": SPLIT_LOOP_LINE}
    data = tally_spec_data()
    data["sources"]["editable"] = [TALLY_SOURCE, SPLIT_SOURCE]
    split = (TALLY / "src" / "split_clear.c").read_text(encoding="utf-8")
    context = tally_context(
        tmp_path, data=data, source=TALLY / "src" / "split_counts.c", contracts=json.dumps({"loops": entries}), extra={SPLIT_SOURCE: split}
    )
    build = built(context)
    assert build.built, build.problem
    assert [item.contract.location for item in build.contracts] == ["src/tally.c:6", "src/tally.c:18", f"{SPLIT_SOURCE}:{SPLIT_LOOP_LINE}"]
    units = [arg.rsplit("/", 1)[-1] for arg in build.argv["vacuity link"] if arg.endswith(".i")]
    assert units == [f"{TRUSTED_MODELS_STEM}.i", f"{TRUSTED_HARNESS_STEM}.i", "editable_00.i", "editable_01.i"]
    second = (build.vacuity.linked.parent / "editable_01.i").read_text(encoding="utf-8")
    assert f'"leanc_loop:{SPLIT_SOURCE}:{SPLIT_LOOP_LINE}"' in second and '"leanc_function:tally_clear"' in second
    assert "leanc_function:tally_below" not in second


CHAR_IS_SIGNED = "char_is_signed"
CHAR_MATCHES_MACRO = "char_matches_macro"


@pytest.mark.parametrize("flags, char_signed", [([], True), (["-funsigned-char"], False)])
def test_inductive_program_has_the_char_signedness_of_compile_flags(tmp_path, flags, char_signed):
    data = tally_spec_data(BELOW)
    data["compile"] = {"flags": flags}
    build = built(tally_context(tmp_path, harness="char_sign.c", data=data))
    assert build.built, build.problem
    verify = cbmc_properties(build.verify.instrumented, checks=False)
    assert holds(verify, CHAR_IS_SIGNED) is char_signed
    assert reached(verify, CHAR_IS_SIGNED) is not char_signed
    assert holds(verify, CHAR_MATCHES_MACRO)


def test_object_bits_build(tmp_path):
    build = built(tally_context(tmp_path, data=tally_spec_data(object_bits=16)))
    assert build.built, build.problem


def test_build_in_the_bwrap_sandbox(tmp_path, needs_bwrap):
    build = built(tally_context(tmp_path, options=VerifyOptions(sandbox_preference="auto")))
    assert build.built, build.problem
    assert {invocation.sandbox for invocation in build.invocations} == {"bwrap"}


def _shape(entries):
    shape = []
    for entry in entries:
        parsed = None
        if SYMBOL_STEP in entry["label"]:
            parsed = sorted(parse_symbol_table(entry["stdout"]))
        elif entry["label"].endswith(LOOP_STEP):
            parsed = [loop.name for loop in parse_loops(entry["stdout"])]
        shape.append((entry["label"], entry["tool"], entry["exit_code"], parsed))
    return shape


def test_recorded_tool_outputs_match_the_installed_tools(tmp_path):
    build, entries = record_build(tally_context(tmp_path))
    assert build.built, build.problem
    assert _shape(entries) == _shape(load_recording())
    assert [mode for mode in BuildMode] == [build.verify.mode, build.vacuity.mode]
    assert {TRUSTED_MODELS_STEM, TRUSTED_HARNESS_STEM} <= {label.split()[-1] for label in build.argv}
