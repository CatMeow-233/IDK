from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from leanc.backends.wp import run_wp
from leanc.backends.wp.argv import MACHDEP
from leanc.backends.wp.compile_flags import (
    MACHDEP_CHAR_SIGNEDNESS,
    classify_wp_compile_flags,
    unapplied_compile_flags,
)
from leanc.backends.wp.installation import tool_path
from leanc.backends.wp.verdict import Verdict, within_build_semantics
from leanc.cli import main
from leanc.foundation.models import StageResult, VerificationResult
from leanc.foundation.status import Outcome, Status
from leanc.pipeline.controller import verify
from leanc.pipeline.repair_loop import proof_repairable
from leanc.specs.compile_options import CharSignedness
from leanc.specs.loader import load_spec
from leanc.toolchain.context import make_context
from leanc.toolchain.framac import find_framac

DEFAULT_FLAGS = ["-Wall", "-Wextra", "-Wpedantic"]
SIGNED_CHAR_SPELLINGS = ["-fsigned-char", "-fno-unsigned-char", "--signed-char"]
UNSIGNED_CHAR_SPELLINGS = ["-funsigned-char", "-fno-signed-char", "--unsigned-char"]
NO_EFFECT_FLAGS = ["-Wall", "-Wno-unused-parameter", "-w", "--no-warnings", "-pedantic", "-pedantic-errors", "-g", "-ggdb3"]
UNAPPLIED_FLAGS = [
    "-DFOO=1",
    "--define-macro=FOO",
    "-UFOO",
    "--undefine-macro=FOO",
    "-Iextra",
    "--include-directory=extra",
    "-O2",
    "-fwrapv",
    "-ffast-math",
    "-fno-strict-aliasing",
    "-mavx2",
    "-includepre.h",
    "-Wdeprecated",
    "-Wno-deprecated",
]
UNSIGNED_CHAR_BUILD = ["-Wall", "-funsigned-char"]
WP_EXAMPLE = "wp_safe_array_get"
CHAR_UNSIGNED_MACRO = "__CHAR_UNSIGNED__"
TARGET = "char_value"
SIGNED_CHAR_RESULT = -56
CHAR_HEADER = f"""#ifndef CHAR_VALUE_H
#define CHAR_VALUE_H
/*@ requires 0 <= k <= 10;
    assigns \\nothing;
    ensures \\result == k + ({SIGNED_CHAR_RESULT}); */
int {TARGET}(int k);
#endif
"""
CHAR_SOURCE = f"""#include "char_value.h"

int {TARGET}(int k)
{{
    char c = (char)(200 + k);
    return c;
}}
"""
CHAR_DRIVER = '#include <stdio.h>\n#include "char_value.h"\nint main(void) { printf("%d", char_value(0)); return 0; }\n'
MODEL_ONLY = "only under Frama-C's own model"


@pytest.mark.parametrize(
    "flags",
    [DEFAULT_FLAGS, NO_EFFECT_FLAGS, []],
)
def test_diagnostic_and_debug_flags_have_no_semantic_effect_for_wp(flags):
    classified = classify_wp_compile_flags(flags)
    assert classified.no_semantic_effect == tuple(flags)
    assert classified.applied == () and classified.not_applied == ()


@pytest.mark.parametrize("flag", SIGNED_CHAR_SPELLINGS)
def test_signed_char_request_is_applied_by_the_signed_char_machdep(flag):
    classified = classify_wp_compile_flags(["-Wall", flag])
    assert classified.applied == (flag,) and classified.not_applied == ()
    assert classified.machdep == MACHDEP and classified.machdep_char_signedness is CharSignedness.SIGNED


@pytest.mark.parametrize("flag", UNSIGNED_CHAR_SPELLINGS)
def test_unsigned_char_request_is_not_applied_by_the_signed_char_machdep(flag):
    classified = classify_wp_compile_flags(["-Wall", flag])
    assert classified.not_applied == (flag,) and classified.applied == ()


@pytest.mark.parametrize(
    ("flags", "applied"),
    [
        (["-funsigned-char", "-fsigned-char"], True),
        (["--unsigned-char", "-fno-unsigned-char"], True),
        (["-fsigned-char", "-funsigned-char"], False),
        (["--signed-char", "-fno-signed-char"], False),
    ],
)
def test_the_last_char_signedness_request_decides_for_every_char_entry(flags, applied):
    classified = classify_wp_compile_flags(flags)
    assert (classified.applied, classified.not_applied) == ((tuple(flags), ()) if applied else ((), tuple(flags)))


def test_char_signedness_of_an_unknown_machdep_is_never_applied():
    classified = classify_wp_compile_flags(["-fsigned-char"], machdep="no_such_machdep")
    assert classified.machdep_char_signedness is None and classified.not_applied == ("-fsigned-char",)


@pytest.mark.parametrize(
    ("flags", "applied"),
    [
        (["-std=c11"], True),
        (["--std=c11"], True),
        (["-std=c1x"], True),
        (["-std=iso9899:2011"], True),
        (["-std=c99", "-std=c11"], True),
        (["-std=c11", "-std=c17"], False),
        (["-std=c17"], False),
        (["-std=gnu11"], False),
        (["--std=c99"], False),
    ],
)
def test_standard_entries_are_applied_only_when_frama_c_parses_the_selected_standard(flags, applied):
    classified = classify_wp_compile_flags(flags)
    assert (classified.applied, classified.not_applied) == ((tuple(flags), ()) if applied else ((), tuple(flags)))


@pytest.mark.parametrize("flag", UNAPPLIED_FLAGS)
def test_every_other_semantic_entry_is_not_applied_by_wp(flag):
    classified = classify_wp_compile_flags([*DEFAULT_FLAGS, flag])
    assert classified.not_applied == (flag,)
    assert classified.no_semantic_effect == tuple(DEFAULT_FLAGS) and classified.applied == ()


def test_classification_details_round_trip_the_unapplied_flags():
    details = {"compile_flags": classify_wp_compile_flags(["-Wall", "-fsigned-char", "-O2"]).details()}
    assert details["compile_flags"] == {
        "machdep": MACHDEP,
        "machdep_char_signedness": "signed",
        "applied": ["-fsigned-char"],
        "no_semantic_effect": ["-Wall"],
        "not_applied": ["-O2"],
    }
    assert unapplied_compile_flags(details) == ["-O2"]
    assert unapplied_compile_flags({}) == []


def test_a_proof_with_unapplied_flags_is_not_a_pass():
    proved = Verdict(Outcome.PASS, "all goals proved")
    downgraded = within_build_semantics(proved, ("-funsigned-char",))
    assert downgraded.outcome is Outcome.INCONCLUSIVE
    assert downgraded.summary.startswith(proved.summary) and "-funsigned-char" in downgraded.summary
    assert within_build_semantics(proved, ()) == proved
    for outcome in (Outcome.FAIL, Outcome.INCONCLUSIVE, Outcome.ERROR, Outcome.TIMEOUT):
        verdict = Verdict(outcome, "under the model")
        assert within_build_semantics(verdict, ("-funsigned-char",)) == verdict


def _unknown_with_open_wp(details: dict) -> VerificationResult:
    wp = StageResult(stage="wp", outcome=Outcome.INCONCLUSIVE, status=Status.UNKNOWN, details=details)
    return VerificationResult(run_id="r", status=Status.UNKNOWN, stages=[StageResult(stage="cbmc", outcome=Outcome.PASS), wp])


def test_a_wp_proof_blocked_by_unapplied_flags_is_not_proof_repairable():
    assert proof_repairable(_unknown_with_open_wp({}))
    blocked = {"compile_flags": classify_wp_compile_flags(UNSIGNED_CHAR_BUILD).details()}
    assert not proof_repairable(_unknown_with_open_wp(blocked))


def test_the_machdep_char_signedness_table_matches_frama_c(needs_framac):
    fc = find_framac()
    printed = subprocess.run(
        [fc.frama_c, "-machdep", MACHDEP, "-print-machdep-builtin-macros"],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": tool_path(fc)},
        timeout=120,
    ).stdout
    assert printed.strip()
    unsigned = CHAR_UNSIGNED_MACRO in printed
    assert MACHDEP_CHAR_SIGNEDNESS[MACHDEP] is (CharSignedness.UNSIGNED if unsigned else CharSignedness.SIGNED)


def _char_project(root: Path, flags: list[str]) -> Path:
    for rel, text in (("include/char_value.h", CHAR_HEADER), ("src/char_value.c", CHAR_SOURCE)):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    spec = {
        "leanc_spec": 1,
        "spec_id": "wp-char-signedness",
        "version": 1,
        "title": "char-dependent contract",
        "requirement": "return the value of (char)200",
        "language": {"name": "c", "standard": "c17"},
        "target": {"function": TARGET, "signature": f"int {TARGET}(int k)", "returns": {"type": "int"}},
        "sources": {"editable": ["src/char_value.c"], "headers": ["include/char_value.h"], "include_dirs": ["include"]},
        "compile": {"flags": flags},
        "wp": {"provers": ["alt-ergo"], "prover_timeout_s": 5, "timeout_s": 120},
        "gates": ["compile", "wp"],
    }
    spec_file = root / "spec.yaml"
    spec_file.write_text(yaml.safe_dump(spec, sort_keys=False))
    return spec_file


def _built_char_value(tmp_path: Path, root: Path, flags: list[str]) -> int:
    driver = tmp_path / "driver.c"
    driver.write_text(CHAR_DRIVER)
    program = tmp_path / "char_value"
    compile_argv = ["clang", *flags, "-Iinclude", "src/char_value.c", str(driver), "-o", str(program)]
    subprocess.run(compile_argv, cwd=root, check=True, timeout=120)
    return int(subprocess.run([str(program)], capture_output=True, text=True, check=True, timeout=60).stdout)


@pytest.mark.parametrize(
    ("flags", "status"),
    [(UNSIGNED_CHAR_BUILD, Status.UNKNOWN), (DEFAULT_FLAGS, Status.PROVED), (["-Wall", "-fsigned-char"], Status.PROVED)],
)
def test_wp_proves_only_builds_whose_char_signedness_frama_c_applies(
    flags, status, tmp_path, capsys, needs_clang, needs_framac
):
    root = tmp_path / "project"
    spec_file = _char_project(root, flags)
    assert main(["spec", "approve", str(spec_file), "--by", "reviewer", "--yes"]) == 0
    result = verify(spec_file, project_root=root, run_dir=tmp_path / "run")
    wp = result.stage("wp")
    assert result.status is status, result.reasons
    assert wp.details["frama_c_model_verdict"]["outcome"] == Outcome.PASS.value, wp.summary
    unsigned_build = status is Status.UNKNOWN
    assert (_built_char_value(tmp_path, root, flags) == SIGNED_CHAR_RESULT) is not unsigned_build
    if unsigned_build:
        assert wp.outcome is Outcome.INCONCLUSIVE and unapplied_compile_flags(wp.details) == ["-funsigned-char"]
        assert any("[wp]" in reason and "-funsigned-char" in reason for reason in result.reasons)
        assert any(MODEL_ONLY in assumption for assumption in result.assumptions)
    else:
        assert wp.outcome is Outcome.PASS and unapplied_compile_flags(wp.details) == []


def test_default_flags_keep_the_shipped_wp_example_proved(example_copy, tmp_path, needs_framac):
    root = example_copy(WP_EXAMPLE)
    spec = load_spec(root / "spec.yaml")
    assert spec.compile.flags == DEFAULT_FLAGS
    result = run_wp(make_context(spec, root, tmp_path / "run"))
    assert result.outcome is Outcome.PASS, result.summary
    assert result.details["compile_flags"]["no_semantic_effect"] == DEFAULT_FLAGS
    assert unapplied_compile_flags(result.details) == []
    assert result.details["frama_c_model_verdict"]["outcome"] == Outcome.PASS.value
