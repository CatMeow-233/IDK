from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from leanc.backends.wp.argv import MACHDEP
from leanc.backends.wp.assumptions import compile_statements
from leanc.backends.wp.build_target import (
    NO_COMPILER,
    UNQUERIED_BUILD_TARGET,
    BuildTarget,
    parse_build_target,
    query_build_target,
)
from leanc.backends.wp.compile_flags import classify_wp_compile_flags, unapplied_compile_flags
from leanc.backends.wp.verdict import Verdict, within_build_semantics
from leanc.foundation.models import StageResult, VerificationResult
from leanc.foundation.status import Outcome, Status
from leanc.pipeline.repair_loop import proof_repairable
from leanc.specs.compile_options import CharSignedness
from leanc.specs.loader import load_spec
from leanc.toolchain.context import make_context

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
X86_64_TRIPLE = "x86_64-pc-linux-gnu"
AARCH64_TRIPLE = "aarch64-unknown-linux-gnu"
X86_64_TARGET = BuildTarget(X86_64_TRIPLE, "x86_64")
AARCH64_TARGET = BuildTarget(AARCH64_TRIPLE, "aarch64")
UNKNOWN_TARGET = BuildTarget(None, None, "-dumpmachine failed (exit 1)")
WP_EXAMPLE_SPEC = Path(__file__).resolve().parent.parent / "examples" / "wp_safe_array_get" / "spec.yaml"
FRAMA_C_PARSE_STATEMENT = "Frama-C preprocesses and parses the code as c11"


def _classify(flags: list[str], target: BuildTarget = X86_64_TARGET, **kwargs):
    return classify_wp_compile_flags(flags, target, **kwargs)


def _applied_or_not(classified) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return classified.applied, classified.not_applied


@pytest.mark.parametrize("flags", [DEFAULT_FLAGS, NO_EFFECT_FLAGS, []])
def test_diagnostic_and_debug_flags_have_no_semantic_effect_for_wp(flags):
    classified = _classify(flags)
    assert classified.no_semantic_effect == tuple(flags)
    assert classified.applied == () and classified.not_applied == ()


@pytest.mark.parametrize("flag", SIGNED_CHAR_SPELLINGS)
def test_signed_char_request_is_applied_by_the_signed_char_machdep(flag):
    classified = _classify(["-Wall", flag])
    assert _applied_or_not(classified) == ((flag,), ())
    assert classified.machdep == MACHDEP and classified.machdep_char_signedness is CharSignedness.SIGNED


@pytest.mark.parametrize("flag", UNSIGNED_CHAR_SPELLINGS)
def test_unsigned_char_request_is_not_applied_by_the_signed_char_machdep(flag):
    assert _applied_or_not(_classify(["-Wall", flag])) == ((), (flag,))


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
    assert _applied_or_not(_classify(flags)) == ((tuple(flags), ()) if applied else ((), tuple(flags)))


def test_char_signedness_of_an_unknown_machdep_is_never_applied():
    classified = _classify(["-fsigned-char"], machdep="no_such_machdep")
    assert classified.machdep_char_signedness is None and classified.not_applied == ("-fsigned-char",)
    default_char = _classify([], machdep="no_such_machdep")
    assert len(default_char.not_applied) == 1 and X86_64_TRIPLE in default_char.not_applied[0]


@pytest.mark.parametrize(
    ("flags", "applied"),
    [
        (["-std=c11"], True),
        (["--std=c11"], True),
        (["-std=c1x"], True),
        (["-std=iso9899:2011"], True),
        (["-std=c17"], True),
        (["--std=c17"], True),
        (["-std=c18"], True),
        (["-std=iso9899:2017"], True),
        (["-std=iso9899:2018"], True),
        (["-std=c99", "-std=c17"], True),
        (["-std=c17", "-std=gnu17"], False),
        (["-std=gnu11"], False),
        (["-std=gnu17"], False),
        (["--std=c99"], False),
        (["-std="], False),
    ],
)
def test_standard_entries_are_applied_only_when_frama_c_parses_the_selected_standard(flags, applied):
    assert _applied_or_not(_classify(flags)) == ((tuple(flags), ()) if applied else ((), tuple(flags)))


@pytest.mark.parametrize("flag", UNAPPLIED_FLAGS)
def test_every_other_semantic_entry_is_not_applied_by_wp(flag):
    classified = _classify([*DEFAULT_FLAGS, flag])
    assert classified.not_applied == (flag,)
    assert classified.no_semantic_effect == tuple(DEFAULT_FLAGS) and classified.applied == ()


def test_classification_details_round_trip_the_unapplied_flags():
    details = {"compile_flags": _classify(["-Wall", "-fsigned-char", "-O2"]).details()}
    assert details["compile_flags"] == {
        "machdep": MACHDEP,
        "machdep_char_signedness": "signed",
        "build_target": {"triple": X86_64_TRIPLE, "architecture": "x86_64", "problem": None},
        "build_char_signedness": "signed",
        "applied": ["-fsigned-char"],
        "no_semantic_effect": ["-Wall"],
        "not_applied": ["-O2"],
    }
    assert unapplied_compile_flags(details) == ["-O2"]
    assert unapplied_compile_flags({}) == []


@pytest.mark.parametrize("triple", [X86_64_TRIPLE, "i386-pc-linux-gnu", "x86_64-pc-linux-gnux32"])
def test_a_signed_char_target_default_is_applied_without_a_flag(triple):
    classified = _classify(DEFAULT_FLAGS, parse_build_target(triple + "\n"))
    assert classified.not_applied == () and classified.build_char_signedness is CharSignedness.SIGNED


@pytest.mark.parametrize(
    "triple", [AARCH64_TRIPLE, "arm64-apple-darwin23.0.0", "powerpc64le-unknown-linux-gnu", "i686-pc-linux-gnu"]
)
def test_a_target_default_not_known_to_be_signed_is_not_applied(triple):
    classified = _classify(DEFAULT_FLAGS, parse_build_target(triple))
    assert len(classified.not_applied) == 1 and classified.applied == ()
    assert triple in classified.not_applied[0] and "not known to be signed" in classified.not_applied[0]
    assert classified.build_char_signedness is None


@pytest.mark.parametrize("flag", SIGNED_CHAR_SPELLINGS)
def test_an_explicit_signed_char_flag_is_applied_on_an_unsigned_default_target(flag):
    classified = _classify(["-Wall", flag], AARCH64_TARGET)
    assert _applied_or_not(classified) == ((flag,), ())
    assert classified.build_char_signedness is CharSignedness.SIGNED


def test_an_explicit_unsigned_char_flag_is_listed_once_on_an_unsigned_default_target():
    assert _applied_or_not(_classify(UNSIGNED_CHAR_BUILD, AARCH64_TARGET)) == ((), ("-funsigned-char",))


@pytest.mark.parametrize("target", [UNKNOWN_TARGET, UNQUERIED_BUILD_TARGET])
@pytest.mark.parametrize("flags", [DEFAULT_FLAGS, ["-fsigned-char"]])
def test_an_unknown_build_target_is_never_applied(target, flags):
    classified = _classify(flags, target)
    assert classified.not_applied[-1] == f"(build target unknown: {target.problem})"


@pytest.mark.parametrize(
    "output", ["", "\n", "x86_64", "x86_64-pc-linux-gnu\naarch64-unknown-linux-gnu\n", "error: no such option", "-pc-linux"]
)
def test_unparsable_dumpmachine_output_gives_no_build_target(output):
    target = parse_build_target(output)
    assert target.triple is None and target.architecture is None and target.problem


def test_dumpmachine_output_names_the_architecture():
    assert parse_build_target(AARCH64_TRIPLE + "\n") == AARCH64_TARGET


def test_a_missing_compiler_gives_no_build_target(tmp_path):
    spec = load_spec(WP_EXAMPLE_SPEC)
    ctx = make_context(spec, tmp_path, tmp_path / "run")
    ctx.tools = dataclasses.replace(ctx.tools, clang=None, clang_version=None)
    target, invocations = query_build_target(ctx, "wp")
    assert target == BuildTarget(None, None, NO_COMPILER) and invocations == []


def test_a_c17_build_standard_is_disclosed_like_language_standard_c17():
    spec = load_spec(WP_EXAMPLE_SPEC)
    spec.language.standard = "c11"
    spec.compile.flags = ["-Wall", "-std=c17"]
    statements = compile_statements(spec, _classify(spec.compile.flags))
    assert f"C language standard c17: {FRAMA_C_PARSE_STATEMENT}" in statements
    assert not any(statement.startswith("compile.flags not applied") for statement in statements)
    spec.compile.flags = ["-Wall"]
    assert not any(FRAMA_C_PARSE_STATEMENT in s for s in compile_statements(spec, _classify(spec.compile.flags)))


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


@pytest.mark.parametrize(("flags", "target"), [(UNSIGNED_CHAR_BUILD, X86_64_TARGET), (DEFAULT_FLAGS, AARCH64_TARGET)])
def test_a_wp_proof_blocked_by_unapplied_flags_is_not_proof_repairable(flags, target):
    assert proof_repairable(_unknown_with_open_wp({}))
    blocked = {"compile_flags": _classify(flags, target).details()}
    assert not proof_repairable(_unknown_with_open_wp(blocked))
