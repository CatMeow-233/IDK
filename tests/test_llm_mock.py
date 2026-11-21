"""MockProvider (fixture replay) and the provider factory."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from leanc.foundation.errors import ProviderError
from leanc.foundation.models import (
    CounterexampleInput,
    FailureReport,
    Location,
    StageResult,
    TraceStep,
    VerificationResult,
)
from leanc.foundation.status import Outcome, Status
from leanc.llm.base import CodeGenRequest, LLMProvider, RepairRequest, SpecDraftRequest
from leanc.llm.factory import available_providers, get_provider
from leanc.llm.mock import MockProvider
from leanc.specs.loader import load_spec, spec_to_yaml

FIXTURE = Path(__file__).parent / "fixtures" / "llm" / "project"


@pytest.fixture
def project(tmp_path) -> Path:
    dst = tmp_path / "proj"
    shutil.copytree(FIXTURE, dst)
    return dst


@pytest.fixture
def spec(project):
    return load_spec(project / "mock" / "spec_draft.yaml")


@pytest.fixture
def mock(project) -> MockProvider:
    return MockProvider(project / "mock")


def _repair_req(spec, round_no: int) -> RepairRequest:
    return RepairRequest(
        requirement=spec.requirement,
        spec=spec,
        spec_yaml=spec_to_yaml(spec),
        round=round_no,
        sources={"src/safe_array.c": "int x;\n"},
        headers={},
        harness=None,
        tests=None,
        diagnostics=[],
        failures=[],
        history=[],
    )


def _failure() -> FailureReport:
    return FailureReport(
        id="cbmc:safe_array_get.pointer_dereference",
        kind="counterexample",
        tool="cbmc",
        property_id="safe_array_get.pointer_dereference.5",
        description="dereference failure: pointer outside object bounds in arr[idx]",
        location=Location(file="src/safe_array.c", line=12, function="safe_array_get"),
        inputs=[CounterexampleInput(name="len", value="3"), CounterexampleInput(name="idx", value="3")],
        call_stack=["main", "safe_array_get"],
        trace=[TraceStep(kind="assignment", location=Location(file="harness/h.c", line=20), text="idx=3")],
    )


def _result(status: Status, failures: list[FailureReport]) -> VerificationResult:
    return VerificationResult(
        run_id="r1",
        spec_id="safe-array-get",
        status=status,
        reasons=["cbmc: 1 property violated"] if failures else [],
        stages=[
            StageResult(stage="compile", outcome=Outcome.PASS),
            StageResult(stage="cbmc", outcome=Outcome.FAIL if failures else Outcome.PASS, failures=failures),
        ],
    )


# ------------------------------------------------------------------ identity
def test_info_marks_mock_as_not_an_llm(mock):
    assert isinstance(mock, LLMProvider)
    assert mock.info.name == "mock" and mock.info.model is None and mock.info.real_llm is False
    assert mock.info.model_dump() == {"name": "mock", "model": None, "real_llm": False}
    assert "real_llm=False" in repr(mock)


# ------------------------------------------------------------------ spec draft
def test_spec_draft_replays_fixture(mock, project):
    d = mock.generate_spec_draft(SpecDraftRequest(requirement="r", spec_id="x"))
    assert d.spec_yaml == (project / "mock/spec_draft.yaml").read_text()
    assert d.harness_c == (project / "mock/harness_draft.c").read_text()
    assert d.tests_c == (project / "mock/tests_draft.c").read_text()
    assert "not LLM output" in d.notes
    (project / "mock/tests_draft.c").unlink()
    assert mock.generate_spec_draft(SpecDraftRequest(requirement="r", spec_id="x")).tests_c is None


def test_spec_draft_missing_or_invalid(mock, project):
    (project / "mock/spec_draft.yaml").write_text("- a\n- b\n")
    with pytest.raises(ProviderError, match="mapping"):
        mock.generate_spec_draft(SpecDraftRequest(requirement="r", spec_id="x"))
    (project / "mock/spec_draft.yaml").write_text("a: [\n")
    with pytest.raises(ProviderError, match="not valid YAML"):
        mock.generate_spec_draft(SpecDraftRequest(requirement="r", spec_id="x"))
    (project / "mock/spec_draft.yaml").unlink()
    with pytest.raises(ProviderError, match="spec_draft.yaml"):
        mock.generate_spec_draft(SpecDraftRequest(requirement="r", spec_id="x"))


# ------------------------------------------------------------------ generation
def test_generate_code_returns_requested_targets(mock, project, spec):
    req = CodeGenRequest(spec=spec, spec_yaml=spec_to_yaml(spec), headers={}, targets=["src/safe_array.c"])
    out = mock.generate_code(req)
    assert out.files == {"src/safe_array.c": (project / "mock/generate/src/safe_array.c").read_text()}
    assert json.loads(out.raw_response) == {"files": out.files}
    assert out == mock.generate_code(req)  # deterministic


def test_generate_code_missing_target_and_traversal(mock, spec):
    req = CodeGenRequest(spec=spec, spec_yaml="", headers={}, targets=["src/safe_array.c", "src/other.c"])
    with pytest.raises(ProviderError, match=r"src/other\.c"):
        mock.generate_code(req)
    req.targets = ["../outside.c"]
    with pytest.raises(ProviderError, match="invalid fixture path"):
        mock.generate_code(req)


def test_symlink_escape_is_refused(mock, project, spec, tmp_path):
    outside = tmp_path / "secret.c"
    outside.write_text("int secret;\n")
    (project / "mock/generate/src/linked.c").symlink_to(outside)
    req = CodeGenRequest(spec=spec, spec_yaml="", headers={}, targets=["src/linked.c"])
    with pytest.raises(ProviderError, match="outside the fixture directory"):
        mock.generate_code(req)


def test_example_mock_generates_the_shipped_fixture(example_copy):
    proj = example_copy("safe_array_get")
    spec = load_spec(proj / "spec.yaml")
    prov = get_provider("mock", project_root=proj)
    out = prov.generate_code(
        CodeGenRequest(spec=spec, spec_yaml=spec_to_yaml(spec), headers={}, targets=list(spec.sources.editable))
    )
    assert out.files["src/safe_array.c"] == (proj / "mock/generate/src/safe_array.c").read_text()


# ------------------------------------------------------------------ repair
def test_propose_patch_files_round(mock, project, spec):
    prop = mock.propose_patch(_repair_req(spec, 1))
    fixture = json.loads((project / "mock/repair/round-1.json").read_text())
    assert prop.files == fixture["files"] and prop.patch is None
    assert prop.root_cause == fixture["root_cause"] and prop.rationale == fixture["rationale"]
    assert prop.note == "mock: replayed fixture repair/round-1.json"
    assert prop.raw_response == (project / "mock/repair/round-1.json").read_text()


def test_propose_patch_diff_round(mock, spec):
    prop = mock.propose_patch(_repair_req(spec, 2))
    assert prop.files is None and prop.patch.startswith("--- a/src/safe_array.c\n+++ b/src/safe_array.c\n")
    assert prop.note.endswith("; diff form")


def test_patch_fixture_applies_to_generated_code(mock, project, spec):
    patch_mod = pytest.importorskip("leanc.repair.patch")
    buggy = {"src/safe_array.c": (project / "mock/generate/src/safe_array.c").read_text()}
    via_patch = patch_mod.proposal_to_contents(None, mock.propose_patch(_repair_req(spec, 2)).patch, buggy)
    via_files = mock.propose_patch(_repair_req(spec, 1)).files
    for fixed in (via_patch["src/safe_array.c"], via_files["src/safe_array.c"]):
        assert "if (idx >= len)" in fixed and "if (idx > len)" not in fixed


def test_missing_round_gives_empty_proposal(mock, spec):
    prop = mock.propose_patch(_repair_req(spec, 3))
    assert prop.is_empty and prop.files is None and prop.patch is None
    assert prop.note == "mock: no fixture for round 3"


def test_missing_fixture_dir_is_not_fatal_for_repair(tmp_path, spec):
    prop = MockProvider(tmp_path / "nope").propose_patch(_repair_req(spec, 1))
    assert prop.note == "mock: no fixture for round 1"


@pytest.mark.parametrize(
    "content, match",
    [
        ("{not json", "not valid JSON"),
        ("[1, 2]", "JSON object"),
        ('{"root_cause": "r", "rationale": "q", "files": ["src/a.c"]}', "must be an object"),
        ('{"root_cause": "r", "rationale": "q", "files": {"a.c": "x"}, "patch": "p"}', "not both"),
        ('{"root_cause": "r", "rationale": "q", "file": {"a.c": "x"}}', "unexpected key"),
        ('{"rationale": "q", "files": {"a.c": "x"}}', "missing required"),
        ('{"root_cause": "r", "rationale": "q"}', "non-empty 'files'"),
        ('{"root_cause": 1, "rationale": "q", "files": {"a.c": "x"}}', "must be a string"),
    ],
)
def test_malformed_repair_fixture_is_an_error(mock, project, spec, content, match):
    (project / "mock/repair/round-1.json").write_text(content)
    with pytest.raises(ProviderError, match=match) as ei:
        mock.propose_patch(_repair_req(spec, 1))
    assert "round-1.json" in str(ei.value)


# ------------------------------------------------------------------ explanations
def test_explain_uses_fixture_or_deterministic_summary(mock, project):
    vr = _result(Status.COUNTEREXAMPLE_FOUND, [_failure()])
    assert mock.explain_failure(vr) == (project / "mock/explain.md").read_text()
    (project / "mock/explain.md").unlink()
    text = mock.explain_failure(vr)
    assert text == mock.explain_failure(vr)
    assert "COUNTEREXAMPLE_FOUND" in text and "[cbmc] counterexample" in text
    assert "src/safe_array.c:12" in text and "idx=3" in text and "cbmc=FAIL" in text
    assert "not LLM output" in text
    ok = mock.explain_failure(_result(Status.VERIFIED_BOUNDED, []))
    assert "No failures" in ok and "not a proof" in ok


def test_analyze_counterexample_is_deterministic(mock, spec):
    src = "\n".join(f"line {i}" for i in range(1, 20))
    text = mock.analyze_counterexample(_failure(), {"src/safe_array.c": src}, spec)
    assert text == mock.analyze_counterexample(_failure(), {"src/safe_array.c": src}, spec)
    assert "safe_array_get" in text and "len=3, idx=3" in text
    assert ">12 | line 12" in text  # numbered excerpt around the failure line


# ------------------------------------------------------------------ factory
def test_factory_mock_paths(project):
    p = get_provider("mock", project_root=project)
    assert isinstance(p, MockProvider) and p.fixture_dir == project / "mock"
    assert get_provider("  MOCK ", project_root=project, mock_dir=Path("other")).fixture_dir == project / "other"
    absolute = project / "abs"
    assert get_provider("mock", project_root=project, mock_dir=absolute).fixture_dir == absolute


def test_factory_unknown_provider_lists_choices(project):
    with pytest.raises(ProviderError) as ei:
        get_provider("gpt-9000", project_root=project)
    for name in available_providers():
        assert name in str(ei.value)
    assert available_providers() == ["mock", "openai", "openai-compatible"]


def test_factory_openai_from_env(project, monkeypatch, tmp_path):
    from leanc.llm.openai_compat import OpenAICompatProvider

    with pytest.raises(ProviderError, match="LEANC_MODEL"):
        get_provider("openai", project_root=project)
    monkeypatch.setenv("LEANC_MODEL", "some-model")
    monkeypatch.setenv("LEANC_API_KEY", "sk-factorytest-0123456789abcdef")
    for name in ("openai", "openai-compatible"):
        p = get_provider(name, project_root=project, transcript_dir=tmp_path / "llm")
        assert isinstance(p, OpenAICompatProvider)
        assert p.info.model_dump() == {"name": "openai-compatible", "model": "some-model", "real_llm": True}
        assert p.transcript_dir == tmp_path / "llm"
        assert os.environ["LEANC_API_KEY"] not in repr(p)
