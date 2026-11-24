from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from leanc.foundation.errors import (
    JSON_LOAD_ERRORS,
    YAML_LOAD_ERRORS,
    ProviderError,
    WorkspaceError,
)
from leanc.foundation.inductive_scope import COVERED_EXECUTIONS, HARNESS_NARROWING, UNREACHED_STATES
from leanc.foundation.models import FailureReport, ProviderInfo, VerificationResult
from leanc.foundation.workspace import normalize_relpath
from leanc.llm.base import (
    CodeGenRequest,
    GeneratedCode,
    LLMProvider,
    RepairProposal,
    RepairRequest,
    SpecDraft,
    SpecDraftRequest,
)
from leanc.llm.parsing import repair_proposal_from_obj
from leanc.llm.prompts.rendering import render_failure
from leanc.specs.model import MANDATORY_CHECKS, Spec

MAX_FIXTURE_BYTES = 4 * 1024 * 1024
MOCK_DISCLAIMER = "mock provider: replayed fixture, not LLM output"

SPEC_DRAFT_FIXTURE = "spec_draft.yaml"
HARNESS_DRAFT_FIXTURE = "harness_draft.c"
TESTS_DRAFT_FIXTURE = "tests_draft.c"
GENERATION_FIXTURE_DIR = "generate"
EXPLANATION_FIXTURE = "explain.md"
MAX_SUMMARY_INPUTS = 10
INDUCTIVE_SUMMARY_SCOPE = (
    f"VERIFIED_INDUCTIVE holds only in this sense: {COVERED_EXECUTIONS} and the mandatory check classes only "
    f"({', '.join(MANDATORY_CHECKS)}), with every contracted loop proved by induction; static objects start "
    f"arbitrary and allocations are assumed to succeed; {HARNESS_NARROWING}; functions that are not claimed, "
    f"{UNREACHED_STATES} and the extra verification.checks classes have only the bounded CBMC result; known gaps: "
    "a memcpy, memmove or memset that overflows into a neighbouring field of the same object, or that writes into "
    "a string literal, is not reported, and library models such as strlen make the gate fail closed; it is not "
    "PROVED."
)


def repair_fixture(round_number: int) -> str:
    return f"repair/round-{int(round_number)}.json"


class MockProvider(LLMProvider):
    def __init__(self, fixture_dir: Path):
        self.fixture_dir = Path(fixture_dir)
        self.info = ProviderInfo(name="mock", model=None, real_llm=False)

    def _contained_fixture_path(self, rel: str) -> Path:
        try:
            norm = normalize_relpath(rel)
        except WorkspaceError as exc:
            raise ProviderError(f"mock: invalid fixture path {rel!r}: {exc}") from None
        path = self.fixture_dir / norm
        root = Path(os.path.realpath(self.fixture_dir))
        real = Path(os.path.realpath(path))
        if real != root and root not in real.parents:
            raise ProviderError(f"mock: fixture {norm} resolves outside the fixture directory {self.fixture_dir}")
        return path

    def _read_optional_fixture(self, rel: str) -> str | None:
        path = self._contained_fixture_path(rel)
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size > MAX_FIXTURE_BYTES:
            raise ProviderError(f"mock: fixture {rel} too large ({size} bytes)")
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ProviderError(f"mock: cannot read fixture {path}: {exc}") from None

    def _where(self, rel: str) -> str:
        return str(self.fixture_dir / rel)

    def generate_spec_draft(self, req: SpecDraftRequest) -> SpecDraft:
        text = self._read_optional_fixture(SPEC_DRAFT_FIXTURE)
        if text is None:
            raise ProviderError(
                f"mock: no spec draft fixture at {self._where(SPEC_DRAFT_FIXTURE)} "
                "(the mock provider only replays fixtures; use --provider openai for a real model)"
            )
        try:
            data = yaml.safe_load(text)
        except YAML_LOAD_ERRORS as exc:
            raise ProviderError(f"mock: spec_draft.yaml is not valid YAML: {exc}") from None
        if not isinstance(data, dict):
            raise ProviderError("mock: spec_draft.yaml must contain a YAML mapping")
        return SpecDraft(
            spec_yaml=text,
            harness_c=self._read_optional_fixture(HARNESS_DRAFT_FIXTURE),
            tests_c=self._read_optional_fixture(TESTS_DRAFT_FIXTURE),
            notes=f"{MOCK_DISCLAIMER} ({self._where(SPEC_DRAFT_FIXTURE)}); requested spec_id {req.spec_id}",
            raw_response=text,
        )

    def generate_code(self, req: CodeGenRequest) -> GeneratedCode:
        files: dict[str, str] = {}
        missing: list[str] = []
        for target in req.targets:
            content = self._read_optional_fixture(f"{GENERATION_FIXTURE_DIR}/{target}")
            if content is None:
                missing.append(target)
            else:
                files[normalize_relpath(target)] = content
        if missing:
            raise ProviderError(
                f"mock: no generation fixture for {missing} (expected {self._where(GENERATION_FIXTURE_DIR)}/<path>)"
            )
        return GeneratedCode(
            files=files,
            notes=f"{MOCK_DISCLAIMER} ({self._where(GENERATION_FIXTURE_DIR)})",
            raw_response=json.dumps({"files": files}, indent=2, ensure_ascii=False, sort_keys=True),
        )

    def propose_patch(self, req: RepairRequest) -> RepairProposal:
        rel = repair_fixture(req.round)
        text = self._read_optional_fixture(rel)
        if text is None:
            return RepairProposal(root_cause="", rationale="", note=f"mock: no fixture for round {req.round}")
        try:
            obj = json.loads(text)
        except JSON_LOAD_ERRORS as exc:
            raise ProviderError(f"mock: {self._where(rel)} is not valid JSON: {exc}") from None
        if not isinstance(obj, dict):
            raise ProviderError(f"mock: {self._where(rel)} must contain a JSON object")
        try:
            prop = repair_proposal_from_obj(obj, strict=True, raw=text)
        except ProviderError as exc:
            raise ProviderError(f"mock: {self._where(rel)}: {exc}") from None
        prop.note = f"mock: replayed fixture {rel}" + (f"; {prop.note}" if prop.note else "")
        return prop

    def analyze_counterexample(self, failure: FailureReport, sources: dict[str, str], spec: Spec) -> str:
        return (
            f"[{MOCK_DISCLAIMER}: deterministic summary of the tool report]\n"
            f"Target {spec.target.function} ({spec.spec_id} v{spec.version}).\n"
            + render_failure(failure, sources=sources)
        )

    def explain_failure(self, result: VerificationResult) -> str:
        text = self._read_optional_fixture(EXPLANATION_FIXTURE)
        if text is not None:
            return text
        return deterministic_summary(result)


def deterministic_summary(result: VerificationResult) -> str:
    status = result.status.value
    lines = [f"[{MOCK_DISCLAIMER}: deterministic summary]", f"Verification status: {status}."]
    if result.reasons:
        lines.append("Reasons: " + "; ".join(result.reasons))
    if result.stages:
        lines.append("Stages: " + ", ".join(f"{s.stage}={s.outcome.value}" for s in result.stages))
    failures = result.all_failures()
    if failures:
        lines.append(f"{len(failures)} failure(s):")
        for f in failures:
            where = f" at {f.location.short()}" if f.location else ""
            shown = ", ".join(f"{i.name}={i.value}" for i in f.inputs[:MAX_SUMMARY_INPUTS])
            inputs = f" (inputs: {shown})" if f.inputs else ""
            lines.append(f"- [{f.tool}] {f.kind}: {f.description}{where}{inputs}")
    else:
        lines.append("No failures were reported.")
    if status == "VERIFIED_BOUNDED":
        lines.append(
            "VERIFIED_BOUNDED holds only within the approved harness, input bounds and unwind limit; it is not a proof."
        )
    if status == "VERIFIED_INDUCTIVE":
        lines.append(INDUCTIVE_SUMMARY_SCOPE)
    return "\n".join(lines)


__all__ = ["MOCK_DISCLAIMER", "MockProvider", "deterministic_summary"]
