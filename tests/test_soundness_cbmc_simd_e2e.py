from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from leanc.cli import main

CPUINFO = Path("/proc/cpuinfo")
AVX2_FLAGS = ["-mavx2"]
SPEC = "spec.yaml"
EDITABLE = "src/safe_array.c"
PRE_CBMC_STAGES = ("spec", "compile", "integrity", "static_analysis", "sanitizer")
VERIFICATION_RECORD = "verify/verification.json"

MASKSTORE_OVERFLOW_ON_UNTESTED_PATH = """#include <immintrin.h>

#include "safe_array.h"

static void broadcast_store(int *dst, int value)
{
    __m256i all_lanes = _mm256_set1_epi32(-1);
    _mm256_maskstore_epi32(dst, all_lanes, _mm256_set1_epi32(value));
}

int safe_array_get(const int *arr, size_t len, size_t idx, int *out)
{
    if (arr == NULL || out == NULL) {
        return SAFE_ARRAY_ERR_NULL;
    }
    if (idx >= len) {
        return SAFE_ARRAY_ERR_RANGE;
    }
    if (len == 7 && arr[idx] == 7) {
        int scratch[1];
        broadcast_store(scratch, arr[idx]);
    }
    *out = arr[idx];
    return SAFE_ARRAY_OK;
}
"""

CORRECT_SHUFFLE = """#include <immintrin.h>

#include "safe_array.h"

int safe_array_get(const int *arr, size_t len, size_t idx, int *out)
{
    if (arr == NULL || out == NULL) {
        return SAFE_ARRAY_ERR_NULL;
    }
    if (idx >= len) {
        return SAFE_ARRAY_ERR_RANGE;
    }
    __m256i lanes = _mm256_set1_epi32(arr[idx]);
    __m256i reversed = _mm256_shuffle_epi32(lanes, 0x1B);
    *out = _mm256_cvtsi256_si32(reversed);
    return SAFE_ARRAY_OK;
}
"""


@pytest.fixture
def needs_avx2():
    if not CPUINFO.is_file() or " avx2" not in CPUINFO.read_text():
        pytest.skip("CPU without AVX2: the sanitizer gate would execute AVX2 instructions")


def _json_call(capsys, *argv: str) -> tuple[int, dict]:
    rc = main([*argv, "--json"])
    return rc, json.loads(capsys.readouterr().out)


def _verify_template(capsys, tmp_path: Path, *, source: str | None, extra_flags: list[str]) -> dict:
    project = tmp_path / "proj"
    rc, _ = _json_call(capsys, "init", str(project))
    assert rc == 0
    spec_path = project / SPEC
    spec = yaml.safe_load(spec_path.read_text())
    spec["compile"]["flags"] = [*spec["compile"]["flags"], *extra_flags]
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))
    if source is not None:
        (project / EDITABLE).write_text(source)
    rc, _ = _json_call(capsys, "spec", "approve", str(spec_path), "--by", "t", "--yes")
    assert rc == 0
    _, data = _json_call(capsys, "verify", str(spec_path))
    return data


def _stages(data: dict) -> dict[str, dict]:
    record = json.loads((Path(data["run_dir"]) / VERIFICATION_RECORD).read_text())
    return {stage["stage"]: stage for stage in record["stages"]}


def _assert_reached_cbmc(stages: dict[str, dict]) -> None:
    assert {name: stages[name]["outcome"] for name in PRE_CBMC_STAGES} == dict.fromkeys(PRE_CBMC_STAGES, "PASS")


def test_maskstore_overflow_on_untested_path_is_not_verified(capsys, tmp_path, needs_toolchain, needs_avx2):
    data = _verify_template(capsys, tmp_path, source=MASKSTORE_OVERFLOW_ON_UNTESTED_PATH, extra_flags=AVX2_FLAGS)
    stages = _stages(data)
    _assert_reached_cbmc(stages)
    assert data["status"] == "UNKNOWN", data["status"]
    cbmc = stages["cbmc"]
    assert cbmc["outcome"] == "INCONCLUSIVE", cbmc["summary"]
    assert "__builtin_ia32_maskstored256()" in cbmc["summary"]
    assert any("-mavx2" in a and "not applied by CBMC" in a for a in cbmc["details"]["assumptions"])


def test_correct_shuffle_is_not_a_counterexample(capsys, tmp_path, needs_toolchain, needs_avx2):
    data = _verify_template(capsys, tmp_path, source=CORRECT_SHUFFLE, extra_flags=AVX2_FLAGS)
    stages = _stages(data)
    _assert_reached_cbmc(stages)
    assert data["status"] == "UNKNOWN", data["status"]
    cbmc = stages["cbmc"]
    assert cbmc["outcome"] == "INCONCLUSIVE", cbmc["summary"]
    assert "__builtin_ia32_pshufd256()" in cbmc["summary"] and "not counterexamples" in cbmc["summary"]
    assert [f for f in cbmc["failures"] if f["kind"] == "counterexample"] == []
    assert cbmc["details"]["properties_unmodelled"] >= 1 and cbmc["details"]["properties_failed"] == 0


def test_plain_template_stays_verified_bounded(capsys, tmp_path, needs_toolchain):
    data = _verify_template(capsys, tmp_path, source=None, extra_flags=[])
    assert data["status"] == "VERIFIED_BOUNDED"
    cbmc = _stages(data)["cbmc"]
    assert cbmc["outcome"] == "PASS"
    assert cbmc["details"]["bodiless_functions"] == {"modelled": [], "unmodelled": []}
    assert not any("not applied by CBMC" in a for a in cbmc["details"]["assumptions"])
