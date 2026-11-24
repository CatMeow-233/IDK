from __future__ import annotations

import json
import shutil
from pathlib import Path

from leanc.cli import main

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "inductive"
ISET_ACCEPTANCE = FIXTURES / "acceptance" / "iset"
ISET_PIPELINE = FIXTURES / "pipeline" / "iset"
SPEC_TEMPLATE = FIXTURES / "e2e" / "iset_spec.yaml"
SPEC_FILE = "spec.yaml"
SOURCE = "src/iset.c"
HEADER = "include/iset.h"
BOUNDED_HARNESS = "harness/iset_harness.c"
INDUCTIVE_HARNESS = "harness/inductive.c"
CONTRACTS = "inductive/loops.json"
CONTRACTS_FIXTURE = ISET_PIPELINE / "loops.json"
CONTRACTED_LINE = 11
VARIANT_FILES = (SOURCE, HEADER, BOUNDED_HARNESS)
HARNESS_FIXTURES = {INDUCTIVE_HARNESS: ISET_PIPELINE / "harness" / "inductive.c"}


def iset_project(tmp_path: Path, variant: str, *, with_contracts: bool = True) -> Path:
    root = tmp_path / variant
    for rel in VARIANT_FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ISET_ACCEPTANCE / variant / rel, root / rel)
    for rel, source in HARNESS_FIXTURES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, root / rel)
    if with_contracts:
        (root / CONTRACTS).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CONTRACTS_FIXTURE, root / CONTRACTS)
    shutil.copyfile(SPEC_TEMPLATE, root / SPEC_FILE)
    return root


def contracts_text(**changes: object) -> str:
    document = json.loads(CONTRACTS_FIXTURE.read_text(encoding="utf-8"))
    document["loops"][0].update(changes)
    return json.dumps(document, indent=1) + "\n"


def cli(capsys, *argv: str) -> tuple[int, dict]:
    rc = main([*argv, "--json"])
    return rc, json.loads(capsys.readouterr().out)


def approved(capsys, root: Path) -> Path:
    spec = root / SPEC_FILE
    rc, payload = cli(capsys, "spec", "approve", str(spec), "--by", "e2e-reviewer", "--yes")
    assert rc == 0, payload
    return spec
