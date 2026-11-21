from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from leanc.foundation.sandbox import detect_sandbox

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
