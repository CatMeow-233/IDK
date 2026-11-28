from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from leanc.backends.wp import run_wp
from leanc.backends.wp.argv import MACHDEP
from leanc.backends.wp.build_target import DUMP_MACHINE_FLAG
from leanc.backends.wp.compile_flags import MACHDEP_CHAR_SIGNEDNESS, unapplied_compile_flags
from leanc.backends.wp.installation import tool_path
from leanc.cli import main
from leanc.foundation.runner import ProcResult
from leanc.foundation.status import Outcome, Status
from leanc.pipeline.controller import verify
from leanc.specs.compile_options import CharSignedness
from leanc.specs.loader import load_spec
from leanc.toolchain.context import BackendContext, make_context
from leanc.toolchain.framac import find_framac
from leanc.toolchain.tools import find_clang

DEFAULT_FLAGS = ["-Wall", "-Wextra", "-Wpedantic"]
UNSIGNED_CHAR_BUILD = ["-Wall", "-funsigned-char"]
SIGNED_CHAR_BUILD = ["-Wall", "-fsigned-char"]
AARCH64_TRIPLE = "aarch64-unknown-linux-gnu"
HOST_ARCHITECTURE = "x86_64"
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
FAILED_QUERY_EXIT = 1


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
        "requirement": "return the value of (char)(200 + k)",
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
    clang = find_clang()
    assert clang
    compile_argv = [clang, *flags, "-Iinclude", "src/char_value.c", str(driver), "-o", str(program)]
    subprocess.run(compile_argv, cwd=root, check=True, timeout=120)
    return int(subprocess.run([str(program)], capture_output=True, text=True, check=True, timeout=60).stdout)


def _verified(tmp_path: Path, flags: list[str]):
    root = tmp_path / "project"
    spec_file = _char_project(root, flags)
    assert main(["spec", "approve", str(spec_file), "--by", "reviewer", "--yes"]) == 0
    result = verify(spec_file, project_root=root, run_dir=tmp_path / "run")
    wp = result.stage("wp")
    assert wp.details["frama_c_model_verdict"]["outcome"] == Outcome.PASS.value, wp.summary
    return root, result, wp


def _fake_dump_machine(monkeypatch, exit_code: int, stdout: str) -> list[list[str]]:
    real_run = BackendContext.run
    queried: list[list[str]] = []

    def run(self, argv, **kwargs):
        if argv[-1] != DUMP_MACHINE_FLAG:
            return real_run(self, argv, **kwargs)
        queried.append(list(argv))
        return ProcResult(
            argv=list(argv),
            cwd=str(kwargs.get("cwd") or ""),
            exit_code=exit_code,
            signal=None,
            timed_out=False,
            output_truncated=False,
            duration_s=0.0,
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr(BackendContext, "run", run)
    return queried


def _assert_model_only(result, wp, entry: str) -> None:
    assert result.status is Status.UNKNOWN, result.reasons
    assert wp.outcome is Outcome.INCONCLUSIVE and entry in unapplied_compile_flags(wp.details)[-1]
    assert any("[wp]" in reason and entry in reason for reason in result.reasons)
    assert any(MODEL_ONLY in assumption for assumption in result.assumptions)


@pytest.mark.parametrize(
    ("flags", "status"),
    [(UNSIGNED_CHAR_BUILD, Status.UNKNOWN), (DEFAULT_FLAGS, Status.PROVED), (SIGNED_CHAR_BUILD, Status.PROVED)],
)
def test_wp_proves_only_builds_whose_char_signedness_frama_c_applies(
    flags, status, tmp_path, capsys, needs_clang, needs_framac
):
    root, result, wp = _verified(tmp_path, flags)
    assert wp.details["compile_flags"]["build_target"]["architecture"] == HOST_ARCHITECTURE
    unsigned_build = status is Status.UNKNOWN
    assert (_built_char_value(tmp_path, root, flags) == SIGNED_CHAR_RESULT) is not unsigned_build
    if unsigned_build:
        _assert_model_only(result, wp, "-funsigned-char")
    else:
        assert result.status is Status.PROVED, result.reasons
        assert wp.outcome is Outcome.PASS and unapplied_compile_flags(wp.details) == []


def test_an_unsigned_char_target_default_is_not_proved(tmp_path, capsys, monkeypatch, needs_clang, needs_framac):
    queried = _fake_dump_machine(monkeypatch, 0, AARCH64_TRIPLE + "\n")
    _, result, wp = _verified(tmp_path, DEFAULT_FLAGS)
    assert queried and queried[0][1:] == [*DEFAULT_FLAGS, DUMP_MACHINE_FLAG]
    _assert_model_only(result, wp, AARCH64_TRIPLE)


def test_an_explicit_signed_char_flag_is_proved_on_an_unsigned_char_target(
    tmp_path, capsys, monkeypatch, needs_clang, needs_framac
):
    _fake_dump_machine(monkeypatch, 0, AARCH64_TRIPLE + "\n")
    _, result, wp = _verified(tmp_path, SIGNED_CHAR_BUILD)
    assert result.status is Status.PROVED, result.reasons
    assert wp.details["compile_flags"]["build_target"]["triple"] == AARCH64_TRIPLE


@pytest.mark.parametrize(("exit_code", "stdout"), [(FAILED_QUERY_EXIT, ""), (0, "not a triple\n")])
def test_a_failed_build_target_query_is_never_proved(
    exit_code, stdout, tmp_path, capsys, monkeypatch, needs_clang, needs_framac
):
    _fake_dump_machine(monkeypatch, exit_code, stdout)
    _, result, wp = _verified(tmp_path, SIGNED_CHAR_BUILD)
    _assert_model_only(result, wp, "(build target unknown: ")


def test_default_flags_keep_the_shipped_wp_example_proved(example_copy, tmp_path, needs_clang, needs_framac):
    root = example_copy(WP_EXAMPLE)
    spec = load_spec(root / "spec.yaml")
    assert spec.compile.flags == DEFAULT_FLAGS
    result = run_wp(make_context(spec, root, tmp_path / "run"))
    assert result.outcome is Outcome.PASS, result.summary
    assert result.details["compile_flags"]["no_semantic_effect"] == DEFAULT_FLAGS
    assert result.details["compile_flags"]["build_target"]["architecture"] == HOST_ARCHITECTURE
    assert unapplied_compile_flags(result.details) == []
    assert result.details["frama_c_model_verdict"]["outcome"] == Outcome.PASS.value
