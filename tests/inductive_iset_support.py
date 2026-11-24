from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
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


@dataclass(frozen=True)
class IsetClaim:
    spec: Path
    harness: Path
    contracts: Path


RANK_CONTAINS_CLAIM = IsetClaim(SPEC_TEMPLATE, ISET_PIPELINE / "harness" / "inductive.c", CONTRACTS_FIXTURE)
INSERT_CLAIM = IsetClaim(
    spec=FIXTURES / "e2e" / "iset_insert_spec.yaml",
    harness=ISET_PIPELINE / "harness" / "insert.c",
    contracts=ISET_PIPELINE / "insert_loops.json",
)


def _copy(source: Path, root: Path, rel: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, root / rel)


def iset_project(
    tmp_path: Path, variant: str, *, with_contracts: bool = True, claim: IsetClaim = RANK_CONTAINS_CLAIM
) -> Path:
    root = tmp_path / variant
    for rel in VARIANT_FILES:
        _copy(ISET_ACCEPTANCE / variant / rel, root, rel)
    _copy(claim.harness, root, INDUCTIVE_HARNESS)
    if with_contracts:
        _copy(claim.contracts, root, CONTRACTS)
    shutil.copyfile(claim.spec, root / SPEC_FILE)
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
