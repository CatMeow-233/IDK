from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from leanc.foundation.errors import SpecError
from leanc.specs.loader import parse_spec_text

REPO = Path(__file__).resolve().parent.parent
BASE_SPEC = (REPO / "examples/safe_array_get/spec.yaml").read_text()


def _spec(mutate) -> str:
    data = yaml.safe_load(BASE_SPEC)
    mutate(data)
    return json.dumps(data)


@pytest.mark.parametrize(
    "flag", ["-fno-sanitize=address", "-fsanitize-recover=all", "-fsanitize=thread", "-fsanitize-ignorelist=x.txt"]
)
def test_compile_flags_cannot_control_sanitizers(flag):
    def mutate(data):
        data["compile"]["flags"] = ["-Wall", flag]

    with pytest.raises(SpecError, match="tests.sanitizers"):
        parse_spec_text(_spec(mutate), source="spec.json")


@pytest.mark.parametrize(
    "flags",
    [
        ["-Xclang", "-analyzer-disable-all-checks"],
        ["-Xanalyzer", "-analyzer-disable-all-checks"],
        ["--analyzer-no-default-checks"],
        ["-Xclang", "-disable-llvm-passes"],
        ["-mllvm", "-asan-instrument-reads=0"],
        ["-mllvm=-asan-stack=0"],
        ["-Wp,-w"],
        ["-Wl,--wrap=free"],
        ["-Wa,--defsym,x=1"],
        ["-Xlinker", "--wrap=malloc"],
        ["@more-flags.txt"],
        ["--config=extra.cfg"],
        ["-fplugin=x.so"],
        ["-fpass-plugin=x.so"],
        ["-resource-dir=/opt/other"],
        ["-B/opt/other/bin"],
        ["-nostdlib"],
    ],
    ids=" ".join,
)
def test_compile_flags_cannot_control_internal_tools(flags):
    def mutate(data):
        data["compile"]["flags"] = ["-Wall", *flags]

    with pytest.raises(SpecError, match="internal tool"):
        parse_spec_text(_spec(mutate), source="spec.json")


def test_ordinary_compile_flags_are_accepted():
    def mutate(data):
        data["compile"]["flags"] = ["-Wall", "-O2", "-march=native", "-ffast-math", "-flto", "-DX=1", "-Iinc", "-g"]

    assert parse_spec_text(_spec(mutate), source="spec.json").compile.flags[1] == "-O2"


def test_contract_safety_checks_join_the_cbmc_checks():
    def mutate(data):
        data["contract"]["safety"].append({"id": "SAFE9", "text": "no leaks", "checks": ["memory-leak", "bounds"]})
        data["verification"]["checks"] = ["unsigned-overflow"]

    spec = parse_spec_text(_spec(mutate), source="spec.json")
    checks = spec.effective_cbmc_checks()
    assert "memory-leak" in checks and checks.count("bounds") == 1
    assert checks[: len(spec.verification.effective_checks())] == spec.verification.effective_checks()
