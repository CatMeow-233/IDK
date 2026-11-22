from __future__ import annotations

import pytest
from cbmc_replay_support import project_with_compile
from cbmc_support import _fake_tools
from clang_driver_support import cc1_args, minimal_source

from leanc.backends.cbmc import build_cbmc_argv, run_cbmc
from leanc.backends.cbmc.compile_flags import (
    CharSignedness,
    platform_architecture,
    translate_compile_options,
)
from leanc.foundation.status import Outcome
from leanc.specs.model import CompileConfig

GUARD = "LEANC_FLAG_GUARD"
GUARDED_ADD = f"""#include <limits.h>
#include "cbmc_fx.h"

int add(int a, int b)
{{
#ifdef {GUARD}
    if ((b > 0 && a > INT_MAX - b) || (b < 0 && a < INT_MIN - b)) {{
        return 0;
    }}
#endif
    return a + b;
}}
"""
CHAR_DEPENDENT_ADD = """#include "cbmc_fx.h"

int add(int a, int b)
{
    char c = (char)200;
    if (c < 0) {
        return a + b;
    }
    return 0;
}
"""
UNAPPLIED_LINE = "compile.flags not applied by CBMC: "
GCC_LONG_ALIASES = [
    (["-funsigned-char", "--signed-char"], ["-funsigned-char", "-fsigned-char"]),
    (["--unsigned-char"], ["-funsigned-char"]),
    (["--define-macro=WIDTH=4"], ["-DWIDTH=4"]),
    (["-DWIDTH", "--undefine-macro=WIDTH"], ["-DWIDTH", "-UWIDTH"]),
    (["--include-directory=extra"], ["-Iextra"]),
]


def _translate(flags: list[str], defines: list[str] | None = None, standard: str = "c17"):
    return translate_compile_options(CompileConfig(flags=flags, defines=defines or []), standard)


def _add_project(tmp_path, flags: list[str], source: str):
    return project_with_compile(tmp_path, flags=flags, source=source, src="add", harness="add_harness", function="add")


def _unapplied_lines(res) -> list[str]:
    return [a for a in res.details["assumptions"] if a.startswith(UNAPPLIED_LINE)]


def test_macros_follow_compiler_order_and_undefines_cancel():
    translation = _translate(["-DA=1", "-DB", "-UA", "-UPREDEFINED", "-DC"], defines=["D=4", "-DE"])
    assert translation.macro_args == ("-DB", "-DC", "-DD=4", "-DE")
    assert translation.not_applied == ("-UPREDEFINED",)


def test_ndebug_from_flags_or_defines_is_dropped_unless_undefined_again():
    dropped = _translate(["-DNDEBUG"], defines=["NDEBUG=1"])
    assert dropped.macro_args == () and dropped.dropped_defines == ("-DNDEBUG", "NDEBUG=1")
    cancelled = _translate(["-DNDEBUG", "-UNDEBUG"])
    assert cancelled.dropped_defines == () and cancelled.not_applied == ()


def test_include_paths_char_and_standard_are_forwarded():
    translation = _translate(["-Iextra", "-Imore", "-std=gnu99", "-funsigned-char"])
    assert translation.include_args == ("-Iextra", "-Imore")
    assert translation.semantic_args == ("--c99", "--unsigned-char")
    assert translation.standard == "gnu99" and translation.standard_mode.approximation is None
    assert translation.not_applied == () and translation.char_signedness is CharSignedness.UNSIGNED


def test_undefine_cancels_a_function_like_define():
    translation = _translate(["-DTWICE(x)=x", "-UTWICE"])
    assert translation.macro_args == () and translation.not_applied == ()


def test_function_like_ndebug_is_dropped_like_ndebug():
    assert _translate(["-DNDEBUG(x)="]).dropped_defines == ("-DNDEBUG(x)=",)


@pytest.mark.parametrize("flags", [["--std=c99"], ["-std=c11", "--std=c99"]])
def test_long_standard_spellings_select_the_standard(flags):
    translation = _translate(flags, standard="c11")
    assert translation.standard == "c99" and translation.standard_mode.flag == "--c99"
    assert translation.not_applied == ()


def test_long_ansi_spelling_selects_c89():
    assert _translate(["--ansi"]).standard_mode.flag == "--c89"


def test_no_warnings_alias_has_no_semantic_effect():
    translation = _translate(["--no-warnings", "-w"])
    assert translation.no_semantic_effect == ("--no-warnings", "-w") and translation.not_applied == ()


def test_last_char_signedness_flag_wins():
    signed = _translate(["-funsigned-char", "-fsigned-char"])
    assert signed.char_signedness is CharSignedness.SIGNED
    assert "--unsigned-char" not in signed.semantic_args
    assert signed.not_applied_on("x86_64") == []
    assert signed.not_applied_on("arm64") == ["-fsigned-char"]
    assert signed.not_applied_on(None) == ["-fsigned-char"]


@pytest.mark.parametrize(
    "standard, mode, approximate",
    [("c99", "--c99", False), ("c11", "--c11", False), ("c17", "--c11", True), ("gnu89", "--c89", False)],
)
def test_standards_map_to_cbmc_modes(standard, mode, approximate):
    translation = _translate([], standard=standard)
    assert translation.standard_mode.flag == mode
    assert (translation.standard_mode.approximation is not None) is approximate
    assert mode in translation.semantic_args


def test_unsupported_standard_and_semantic_flags_are_not_applied():
    flags = ["-Wall", "-Wextra", "-Wpedantic", "-pedantic", "-g", "-std=c2x", "-O2", "-ffast-math", "-mavx2",
             "-fwrapv", "-m32", "-includepre.h", "-isystemsys", "-D"]
    translation = _translate(flags)
    assert translation.no_semantic_effect == ("-Wall", "-Wextra", "-Wpedantic", "-pedantic", "-g")
    assert translation.not_applied == ("-O2", "-ffast-math", "-mavx2", "-fwrapv", "-m32",
                                       "-includepre.h", "-isystemsys", "-D", "-std=c2x")
    assert translation.standard_mode is None and translation.macro_args == ()


def test_every_entry_is_translated_as_an_option_of_its_own():
    translation = _translate(["-iprefix", "-funsigned-char"])
    assert translation.char_signedness is CharSignedness.UNSIGNED and translation.not_applied == ("-iprefix",)


def test_platform_architecture():
    assert platform_architecture("64-bit x86_64 linux") == "x86_64"
    assert platform_architecture(None) is None and platform_architecture("weird") is None


def test_argv_carries_translated_flags_in_every_mode(tmp_path):
    ctx = project_with_compile(
        tmp_path,
        flags=["-Iextra", "-DFROM_FLAGS=2", "-funsigned-char", "-std=c99", "-O2"],
        defines=["FROM_DEFINES"],
        src="arr_get_ok",
        harness="arr_get_harness",
        function="arr_get",
    )
    _fake_tools(ctx)
    for mode in ("verify", "vacuity", "coverage"):
        argv = build_cbmc_argv(ctx, mode=mode)
        assert argv.index("-Iextra") < argv.index("-Iinclude")
        assert argv.index("-DFROM_FLAGS=2") < argv.index("-DFROM_DEFINES")
        assert "--unsigned-char" in argv and "--c99" in argv
        assert "-O2" not in argv and "-std=c99" not in argv


def test_mutation_macro_flag_reaches_cbmc(tmp_path, needs_cbmc):
    guarded = run_cbmc(_add_project(tmp_path / "defined", [f"-D{GUARD}"], GUARDED_ADD))
    assert guarded.outcome is Outcome.PASS, guarded.summary
    undefined = run_cbmc(_add_project(tmp_path / "undefined", [f"-D{GUARD}", f"-U{GUARD}"], GUARDED_ADD))
    assert undefined.outcome is Outcome.FAIL, undefined.summary


def test_mutation_unsigned_char_flag_reaches_cbmc(tmp_path, needs_cbmc):
    unsigned = run_cbmc(_add_project(tmp_path / "unsigned", ["-funsigned-char"], CHAR_DEPENDENT_ADD))
    assert unsigned.outcome is Outcome.PASS, unsigned.summary
    assert "--unsigned-char" in unsigned.details["argv"]["verify"]
    signed = run_cbmc(_add_project(tmp_path / "signed", ["-funsigned-char", "-fsigned-char"], CHAR_DEPENDENT_ADD))
    assert signed.outcome is Outcome.FAIL, signed.summary
    assert _unapplied_lines(signed) == []


def test_unapplied_flags_are_disclosed_without_changing_the_verdict(tmp_path, needs_cbmc):
    flags = ["-Wall", "-Wextra", "-std=c99", "-Iinclude", "-funsigned-char", "-O2", "-ffast-math", "-mavx2"]
    ctx = project_with_compile(
        tmp_path, flags=flags, src="arr_get_ok", harness="arr_get_harness", function="arr_get", coverage=True
    )
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.PASS, res.summary
    assert set(res.details["argv"]) == {"verify", "vacuity", "coverage"}
    assert all("--unsigned-char" in argv and "--c99" in argv for argv in res.details["argv"].values())
    (line,) = _unapplied_lines(res)
    assert line.startswith(UNAPPLIED_LINE + "-O2, -ffast-math, -mavx2 - ")
    assert "IEEE-754" in line and "fast-math" in line and "-m ISA" in line and "optimisation" in line
    assert "-Wall" not in line
    assert res.details["compile_flags"]["no_semantic_effect"] == ["-Wall", "-Wextra"]
    standard_line = "C language standard c99: checked in CBMC's --c99 mode"
    assert any(a.startswith(standard_line) for a in res.details["assumptions"])


def test_ndebug_in_flags_cannot_disable_harness_assertions(tmp_path, needs_cbmc):
    ctx = project_with_compile(tmp_path, flags=["-DNDEBUG"], src="arr_get_ok", harness="arr_get_harness",
                               function="arr_get")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.PASS, res.summary
    assert not any("NDEBUG" in arg for arg in res.details["argv"]["verify"])
    assert any("would disable harness assertions: -DNDEBUG" in a for a in res.details["assumptions"])


def test_default_flags_add_no_disclosure(tmp_path, needs_cbmc):
    ctx = project_with_compile(tmp_path, flags=["-Wall", "-Wextra", "-Wpedantic"], src="arr_get_ok",
                               harness="arr_get_harness", function="arr_get")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.PASS, res.summary
    assert _unapplied_lines(res) == []
    assert "--c11" in res.details["argv"]["verify"]
    assert any("C language standard c17: checked in CBMC's --c11 mode (CBMC has no C17 mode" in a
               for a in res.details["assumptions"])


@pytest.mark.parametrize("aliased, canonical", GCC_LONG_ALIASES, ids=lambda flags: " ".join(flags))
def test_gcc_long_aliases_translate_like_their_short_spellings(aliased, canonical):
    assert _translate(aliased).details() == _translate(canonical).details()


@pytest.mark.parametrize("aliased, canonical", GCC_LONG_ALIASES, ids=lambda flags: " ".join(flags))
def test_gcc_long_aliases_mean_their_short_spellings_to_clang(needs_clang, tmp_path, aliased, canonical):
    source = minimal_source(tmp_path, ".c")
    assert cc1_args(source, aliased) == cc1_args(source, canonical)


def test_mutation_signed_char_alias_reaches_cbmc(tmp_path, needs_cbmc):
    res = run_cbmc(_add_project(tmp_path, ["-funsigned-char", "--signed-char"], CHAR_DEPENDENT_ADD))
    assert res.outcome is Outcome.FAIL and "--unsigned-char" not in res.details["argv"]["verify"]
