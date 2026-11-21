from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from leanc.cli import main

REPO = Path(__file__).resolve().parent.parent
FIX = REPO / "tests/fixtures/integrity"

TARGETS = {
    "safe_array_get": ("safe_array_get", "src/safe_array.c"),
    "safe_ratio": ("div_by_zero", "src/safe_ratio.c"),
    "sensor_read": ("null_deref", "src/sensor.c"),
    "join_with_sep": ("runtime_memory", "src/join.c"),
}

ATTACK_TECHNIQUES: dict[str, str] = {
    "cand_assertfail.c": "no-op __assert_fail removes every harness assert",
    "cand_nondet.c": "nondet_size_t override narrows the harness input space",
    "cand_pragma2.c": "macro-built _Pragma(CPROVER check disable ...)",
    "cand_clang.c": "#ifdef __clang__: CBMC (gcc preprocessor) sees other code",
    "cand_opt.c": "#ifdef __OPTIMIZE__",
    "cand_exit.c": "exit(0) prunes CBMC paths, fakes test success",
    "cand_leak.c": "constructor + atexit(_exit(0)) beats the oracle and LSan",
    "cand_vacmacro.c": "__CPROVER__ && !LEANC_VACUITY_CHECK -> vacuous verify run",
    "reqs_cheat.c": "",
    "reqs_div.c": "",
    "sec_a1_src.c": "",
    "sec_a2_src.c": "",
    "sec_a3.c": "",
    "sec_a4.c": "GNU linemarker marks the file as a system header",
    "sec_a5.c": "",
    "sec_ab.c": "abort() is assume(false) in CBMC",
    "sec_af.c": "",
    "sec_vf.c": "weak __VERIFIER_assume",
    "r2_vla.c": "input-sized VLA (CBMC has no stack limit) crashes natively",
    "r2_alloca.c": "__builtin_alloca, no import left for nm to see",
}


def _project(tmp_path: Path, fixture: str) -> tuple[Path, str]:
    text = (FIX / fixture).read_text()
    for fn, (example, rel) in TARGETS.items():
        if fn in text:
            dst = tmp_path / example
            shutil.copytree(REPO / "examples" / example, dst, ignore=shutil.ignore_patterns(".leanc"))
            (dst / rel).write_text(text)
            return dst, rel
    pytest.skip(f"no target example for {fixture}")


@pytest.mark.slow
@pytest.mark.parametrize("fixture", list(ATTACK_TECHNIQUES))
def test_attack_is_not_verified(fixture, tmp_path, capsys, needs_toolchain):
    proj, _ = _project(tmp_path, fixture)
    spec = str(proj / "spec.yaml")
    assert main(["spec", "approve", spec, "--by", "reviewer", "--yes"]) == 0
    capsys.readouterr()
    rc = main(["verify", spec, "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["status"] not in ("VERIFIED_BOUNDED", "PROVED"), (fixture, ATTACK_TECHNIQUES[fixture])
    assert rc != 0
    stages = {s["stage"]: s for s in data["verification"]["stages"]}
    caught_by_integrity_or_compile = stages["compile"]["outcome"] != "PASS" or stages["integrity"]["outcome"] != "PASS"
    assert caught_by_integrity_or_compile, (fixture, ATTACK_TECHNIQUES[fixture])
    if stages.get("integrity", {}).get("outcome") == "FAIL":
        assert stages["sanitizer"]["outcome"] == "SKIPPED", "code with forbidden constructs must not be executed"
