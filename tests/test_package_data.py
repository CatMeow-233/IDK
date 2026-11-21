from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "leanc"
DATA = PACKAGE / "data"
IGNORED_PARTS = frozenset({"__pycache__"})


def _package_data_globs() -> list[str]:
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return config["tool"]["setuptools"]["package-data"]["leanc"]


def test_every_data_file_is_shipped_by_a_package_data_glob():
    globs = _package_data_globs()
    unshipped = sorted(
        rel
        for path in DATA.rglob("*")
        if path.is_file() and not set(path.parts) & IGNORED_PARTS
        for rel in [path.relative_to(PACKAGE).as_posix()]
        if not any(fnmatch.fnmatchcase(rel, g) and rel.count("/") == g.count("/") for g in globs)
    )
    assert not unshipped, f"add package-data globs for: {unshipped}"
