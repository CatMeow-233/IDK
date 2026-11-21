from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from integrity_support import DEFAULT_FLAGS, READ_LINE, integrity_gate, shipped, with_flags

from leanc.backends.flag_arity import FlagArityOutcome, flag_arity_argv
from leanc.foundation.errors import SpecError
from leanc.foundation.runner import ProcResult
from leanc.foundation.status import Outcome, Status
from leanc.specs.loader import parse_spec_text

REPO = Path(__file__).resolve().parent.parent
BASE_SPEC = (REPO / "examples/safe_array_get/spec.yaml").read_text()
SIGKILL = 9
BIGFRAME_VLA_READ = (
    "#ifdef BIGFRAME\n    int copy[len];\n    copy[idx] = arr[idx];\n    *out = copy[idx];\n#else\n"
    "    *out = arr[idx];\n#endif"
)
VALUE_SWALLOWING_FLAGS = [
    ("-iprefix", "-funsigned-char"),
    ("--include-directory-after", "-fno-signed-char"),
    ("-iwithprefixbefore", "-funsigned-char"),
    ("-cxx-isystem", "-DLEANC_HIDDEN"),
    ("-I", "-Wno-such-dir"),
    ("-MT", "-Wdeps"),
]
JOINED_SPELLINGS = ["-Iinclude", "-DX=1", "-UX", "-std=c17", "--std=c17", "-isysteminclude", "-xc", "-iquoteinclude"]


def _proc(exit_code: int | None = 0, stderr: str = "", **changes) -> ProcResult:
    fields = {"argv": [], "cwd": "", "exit_code": exit_code, "signal": None, "timed_out": False,
              "output_truncated": False, "duration_s": 0.0, "stdout": "", "stderr": stderr}
    return ProcResult(**{**fields, **changes})


def _spec_with_flags(flags: list[str]) -> str:
    data = yaml.safe_load(BASE_SPEC)
    data["compile"]["flags"] = flags
    return json.dumps(data)


@pytest.mark.parametrize("flags", [["src/other.c"], ["-"], ["--", "src/other.c"], ["-I", "include"]], ids=" ".join)
def test_compile_flags_entries_must_be_options(flags):
    with pytest.raises(SpecError, match="every compile.flags entry must be one option"):
        parse_spec_text(_spec_with_flags(["-Wall", *flags]), source="spec.json")


@pytest.mark.parametrize("flags", VALUE_SWALLOWING_FLAGS, ids=" ".join)
def test_entry_that_takes_the_next_entry_as_its_value_is_an_integrity_error(example_copy, needs_clang, tmp_path, flags):
    result = integrity_gate(example_copy, tmp_path, spec_edits=with_flags("-Wall", *flags))
    assert result.outcome is Outcome.ERROR and result.status is Status.TOOL_ERROR, result.summary
    assert result.details["flag_arity"]["separate_value"] == [flags[0]]
    assert f"compile.flags entry '{flags[0]}' takes the next entry as its value" in result.summary


def test_dangling_option_cannot_hide_a_define_from_the_stack_checks(example_copy, needs_clang, tmp_path):
    edits = {DEFAULT_FLAGS: "flags: [-Wall, -I, -Wno-such-dir]\n  defines: [BIGFRAME]"}
    result = integrity_gate(example_copy, tmp_path, shipped().replace(READ_LINE, BIGFRAME_VLA_READ), edits)
    assert result.outcome is not Outcome.PASS and result.details["flag_arity"]["separate_value"] == ["-I"]


def test_joined_spellings_pass_the_probe(example_copy, needs_clang, tmp_path):
    result = integrity_gate(example_copy, tmp_path, spec_edits=with_flags("-Wall", *JOINED_SPELLINGS))
    assert result.outcome is Outcome.PASS, result.summary
    assert result.details["flag_arity"] == {
        "checked": True,
        "argv": flag_arity_argv(result.details["tools"]["clang"], "<entry>"),
        "separate_value": [],
    }


def test_probe_reads_only_the_missing_value_diagnostic():
    outcome = FlagArityOutcome()
    outcome.judge("-O2", _proc(1, "clang: error: no input files"))
    outcome.judge("-iprefix", _proc(1, "clang: error: argument to '-iprefix' is missing (expected 1 value)"))
    outcome.judge("-sectalign", _proc(1, "clang: error: argument to '-sectalign' is missing (expected 3 values)"))
    assert outcome.separate_value == ["-iprefix", "-sectalign"] and len(outcome.problems) == 2


@pytest.mark.parametrize(
    "proc, problem",
    [
        (_proc(None, signal=SIGKILL), "signal 9"),
        (_proc(0, output_truncated=True), "output truncated"),
        (_proc(None, error="failed to launch"), "failed to launch"),
    ],
    ids=["signal", "truncated", "launch"],
)
def test_probe_that_did_not_finish_cleanly_is_a_problem_not_a_standalone_option(proc, problem):
    outcome = FlagArityOutcome()
    outcome.judge("-O2", proc)
    assert outcome.separate_value == [] and problem in outcome.problems[0]
    assert outcome.details("clang")["checked"] is False


def test_probe_timeout_is_reported_as_a_timeout():
    outcome = FlagArityOutcome()
    outcome.judge("-O2", _proc(None, timed_out=True))
    assert outcome.timeouts and not outcome.problems and outcome.details("clang")["checked"] is False
