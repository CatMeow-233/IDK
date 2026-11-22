from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from leanc.foundation.sandbox import detect_sandbox
from leanc.toolchain.framac import MIN_FRAMAC_MAJOR, find_framac, framac_version_key
from leanc.toolchain.tools import tool_version

CPUINFO = Path("/proc/cpuinfo")
AVX2_CPU_FLAG = " avx2"
REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"


@pytest.fixture(autouse=True)
def _isolated_leanc_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("leanc_home")
    monkeypatch.setenv("LEANC_HOME", str(home))
    for var in ("LEANC_API_KEY", "OPENAI_API_KEY", "LEANC_BASE_URL", "LEANC_MODEL"):
        monkeypatch.delenv(var, raising=False)
    yield home


def _skip_unless(cond: bool, why: str):
    if not cond:
        pytest.skip(why)


@pytest.fixture
def needs_clang():
    _skip_unless(shutil.which("clang") is not None, "clang not installed")


@pytest.fixture
def needs_cbmc():
    _skip_unless(shutil.which("cbmc") is not None, "cbmc not installed")


@pytest.fixture
def needs_bwrap():
    _skip_unless(detect_sandbox("auto").active, "functional bwrap sandbox not available")


def _usable_framac() -> bool:
    fc = find_framac()
    return fc is not None and framac_version_key(tool_version(fc.real, "-version"))[0] >= MIN_FRAMAC_MAJOR


@pytest.fixture
def needs_framac():
    _skip_unless(_usable_framac(), f"Frama-C >= {MIN_FRAMAC_MAJOR} with WP not installed (set LEANC_FRAMAC)")


@pytest.fixture
def needs_avx2():
    has_avx2 = CPUINFO.is_file() and AVX2_CPU_FLAG in CPUINFO.read_text()
    _skip_unless(has_avx2, "CPU without AVX2: the sanitizer gate would execute AVX2 instructions")


@pytest.fixture
def needs_toolchain(needs_clang, needs_cbmc, needs_bwrap):
    return None


@pytest.fixture
def example_copy(tmp_path):
    def _copy(name: str) -> Path:
        src = EXAMPLES / name
        dst = tmp_path / name
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".leanc"))
        return dst

    return _copy
