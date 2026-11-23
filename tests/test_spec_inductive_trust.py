from __future__ import annotations

import json
from pathlib import Path

import pytest
from spec_inductive_support import (
    INDUCTIVE_CONTRACTS,
    INDUCTIVE_HARNESS,
    INDUCTIVE_HARNESS_TEXT,
    SAFE_ARRAY_EXAMPLE,
    SAFE_ARRAY_SOURCE,
    SAFE_ARRAY_SPEC,
    inductive_section,
    spec_data,
    spec_text_with,
    spec_with,
)

from leanc.foundation.errors import ApprovalError, WorkspaceError
from leanc.foundation.workspace import Workspace
from leanc.repair.policy import check_candidate, check_generated
from leanc.rules.source import source_violations
from leanc.specs.approval import approve_spec, check_approval
from leanc.specs.loader import load_spec
from leanc.toolchain.context import make_context

SPEC_FILE = "spec.yaml"
APPROVER = "alice"
CONTRACTS_TEXT = json.dumps({"loops": []}) + "\n"
EDITED_SOURCE_SUFFIX = "\nint safe_array_extra = 0;\n"


@pytest.fixture(scope="module")
def plain_spec():
    return load_spec(SAFE_ARRAY_SPEC)


@pytest.fixture(scope="module")
def inductive_spec():
    return spec_with(inductive_section())


@pytest.fixture(scope="module")
def good_source() -> str:
    return (SAFE_ARRAY_SPEC.parent / SAFE_ARRAY_SOURCE).read_text(encoding="utf-8")


@pytest.fixture
def inductive_project(example_copy) -> Path:
    root = example_copy(SAFE_ARRAY_EXAMPLE)
    (root / SPEC_FILE).write_text(spec_text_with(inductive_section()), encoding="utf-8")
    (root / INDUCTIVE_HARNESS).parent.mkdir(parents=True, exist_ok=True)
    (root / INDUCTIVE_HARNESS).write_text(INDUCTIVE_HARNESS_TEXT, encoding="utf-8")
    return root


def _rules_for(decision, path: str) -> list[str]:
    return [v.rule for v in decision.violations if v.file == path]


def test_repair_cannot_write_the_inductive_harness(inductive_spec, good_source):
    candidate = {SAFE_ARRAY_SOURCE: good_source, INDUCTIVE_HARNESS: "int main(void) { return 0; }\n"}
    decision = check_candidate(inductive_spec, {SAFE_ARRAY_SOURCE: good_source}, candidate)
    assert not decision.allowed
    assert _rules_for(decision, INDUCTIVE_HARNESS) == ["PATH_NOT_EDITABLE"]
    message = next(v.message for v in decision.violations if v.file == INDUCTIVE_HARNESS)
    assert "the trusted inductive harness" in message
    assert INDUCTIVE_CONTRACTS in message and "inductive.contracts" in message


def test_repair_cannot_delete_or_replace_an_existing_inductive_harness(inductive_spec, good_source):
    current = {SAFE_ARRAY_SOURCE: good_source, INDUCTIVE_HARNESS: INDUCTIVE_HARNESS_TEXT}
    weakened = {**current, INDUCTIVE_HARNESS: INDUCTIVE_HARNESS_TEXT.replace("LEANC_REACH", "(void)")}
    assert _rules_for(check_candidate(inductive_spec, current, weakened), INDUCTIVE_HARNESS) == ["PATH_NOT_EDITABLE"]
    deleted = {SAFE_ARRAY_SOURCE: good_source + EDITED_SOURCE_SUFFIX}
    assert _rules_for(check_candidate(inductive_spec, current, deleted), INDUCTIVE_HARNESS) == ["PATH_NOT_EDITABLE"]


def test_generation_cannot_write_the_inductive_harness(inductive_spec, good_source):
    decision = check_generated(inductive_spec, {SAFE_ARRAY_SOURCE: good_source, INDUCTIVE_HARNESS: INDUCTIVE_HARNESS_TEXT})
    assert not decision.allowed
    assert _rules_for(decision, INDUCTIVE_HARNESS) == ["PATH_NOT_EDITABLE"]


def test_contracts_path_is_writable_only_when_the_section_names_it(plain_spec, inductive_spec, good_source):
    old = {SAFE_ARRAY_SOURCE: good_source}
    new = {SAFE_ARRAY_SOURCE: good_source, INDUCTIVE_CONTRACTS: CONTRACTS_TEXT}
    assert "PATH_NOT_EDITABLE" not in _rules_for(check_candidate(inductive_spec, old, new), INDUCTIVE_CONTRACTS)
    assert "PATH_NOT_EDITABLE" not in _rules_for(check_generated(inductive_spec, new), INDUCTIVE_CONTRACTS)
    assert _rules_for(check_candidate(plain_spec, old, new), INDUCTIVE_CONTRACTS) == ["PATH_NOT_EDITABLE"]


@pytest.mark.parametrize("path", [".leanc/approvals/safe-array-get.json", "spec.yaml", "inductive/other.json"])
def test_other_configuration_files_stay_protected_with_the_section(inductive_spec, good_source, path):
    decision = check_candidate(
        inductive_spec, {SAFE_ARRAY_SOURCE: good_source}, {SAFE_ARRAY_SOURCE: good_source, path: CONTRACTS_TEXT}
    )
    assert _rules_for(decision, path) == ["PATH_NOT_EDITABLE"]


@pytest.mark.parametrize(
    "include", ["#include <state_harness.c>", "#include <inductive/state_harness.c>", "#include <loops.json>"]
)
def test_editable_code_cannot_include_the_inductive_files(inductive_spec, good_source, include):
    text = include + "\n" + good_source
    escapes = [v.message for v in source_violations(inductive_spec, SAFE_ARRAY_SOURCE, text) if v.rule == "INCLUDE_ESCAPE"]
    assert len(escapes) == 1 and "names a harness, test or source file" in escapes[0]


def test_cpp_editable_code_cannot_include_the_inductive_files(example_copy):
    root = example_copy("cpp_ring_buffer")
    data = spec_data(root / SPEC_FILE)
    spec = spec_with(inductive_section(functions=[data["target"]["function"]]), data)
    source = data["sources"]["editable"][0]
    text = "#include <loops.json>\n" + (root / source).read_text(encoding="utf-8")
    escapes = [v.message for v in source_violations(spec, source, text) if v.rule == "INCLUDE_ESCAPE"]
    assert len(escapes) == 1 and "names a harness, test or source file" in escapes[0]


def test_approval_hashes_the_inductive_harness_and_editing_it_revokes_approval(inductive_project):
    ws = Workspace(inductive_project)
    spec = load_spec(inductive_project / SPEC_FILE)
    record = approve_spec(spec, SPEC_FILE, ws, approved_by=APPROVER)
    assert INDUCTIVE_HARNESS in record["protected_files"]
    assert INDUCTIVE_CONTRACTS not in record["protected_files"]
    assert check_approval(spec, ws).approved
    harness = inductive_project / INDUCTIVE_HARNESS
    harness.write_text(INDUCTIVE_HARNESS_TEXT + "/* weakened */\n", encoding="utf-8")
    check = check_approval(spec, ws)
    assert not check.approved
    assert f"protected file {INDUCTIVE_HARNESS} was modified since approval (hash mismatch)" in check.reasons


def test_editing_the_contracts_file_keeps_the_approval(inductive_project):
    ws = Workspace(inductive_project)
    spec = load_spec(inductive_project / SPEC_FILE)
    approve_spec(spec, SPEC_FILE, ws, approved_by=APPROVER)
    (inductive_project / INDUCTIVE_CONTRACTS).write_text(CONTRACTS_TEXT, encoding="utf-8")
    assert check_approval(spec, ws).approved


def test_approval_needs_the_inductive_harness(inductive_project):
    (inductive_project / INDUCTIVE_HARNESS).unlink()
    with pytest.raises(WorkspaceError, match="state_harness.c"):
        approve_spec(load_spec(inductive_project / SPEC_FILE), SPEC_FILE, Workspace(inductive_project), approved_by=APPROVER)


def test_adding_the_section_to_an_approved_spec_needs_a_new_approval(example_copy):
    root = example_copy(SAFE_ARRAY_EXAMPLE)
    ws = Workspace(root)
    approve_spec(load_spec(root / SPEC_FILE), SPEC_FILE, ws, approved_by=APPROVER)
    (root / INDUCTIVE_HARNESS).parent.mkdir(parents=True, exist_ok=True)
    (root / INDUCTIVE_HARNESS).write_text(INDUCTIVE_HARNESS_TEXT, encoding="utf-8")
    check = check_approval(spec_with(inductive_section()), ws)
    assert not check.approved
    assert "spec content changed since approval (hash mismatch)" in check.reasons
    assert "set of protected files differs from the approved set" in check.reasons


def test_a_json_spec_file_that_is_the_contracts_path_cannot_be_approved(inductive_project):
    spec = load_spec(inductive_project / SPEC_FILE)
    contracts_spec = inductive_project / INDUCTIVE_CONTRACTS
    contracts_spec.write_text(json.dumps(spec_data(inductive_project / SPEC_FILE)), encoding="utf-8")
    with pytest.raises(ApprovalError, match="is also an editable file of the spec"):
        approve_spec(load_spec(contracts_spec), INDUCTIVE_CONTRACTS, Workspace(inductive_project), approved_by=APPROVER)
    assert approve_spec(spec, SPEC_FILE, Workspace(inductive_project), approved_by=APPROVER)["spec_path"] == SPEC_FILE


def test_snapshot_holds_the_inductive_harness_and_contracts(inductive_project, tmp_path):
    (inductive_project / INDUCTIVE_CONTRACTS).write_text(CONTRACTS_TEXT, encoding="utf-8")
    ctx = make_context(load_spec(inductive_project / SPEC_FILE), inductive_project, tmp_path / "run")
    assert (ctx.snapshot_dir / INDUCTIVE_HARNESS).read_text(encoding="utf-8") == INDUCTIVE_HARNESS_TEXT
    assert (ctx.snapshot_dir / INDUCTIVE_CONTRACTS).read_text(encoding="utf-8") == CONTRACTS_TEXT


def test_missing_contracts_file_is_left_out_of_the_snapshot(inductive_project, tmp_path):
    ctx = make_context(load_spec(inductive_project / SPEC_FILE), inductive_project, tmp_path / "run")
    assert (ctx.snapshot_dir / INDUCTIVE_HARNESS).is_file()
    assert not (ctx.snapshot_dir / INDUCTIVE_CONTRACTS).exists()
