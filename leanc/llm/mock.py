"""Deterministic fixture-replaying provider.

The mock provider lets the whole pipeline (draft -> generate -> verify ->
repair -> report) run offline and reproducibly.  It answers every request from
files under a fixture directory (default ``<project_root>/mock/``)::

    spec_draft.yaml [+ harness_draft.c, tests_draft.c]   generate_spec_draft
    generate/<editable rel path>                         generate_code
    repair/round-<N>.json                                propose_patch (N = 1-based round)
    explain.md                                           explain_failure (optional)

``repair/round-<N>.json`` is ``{"root_cause", "rationale", "files": {path: content}}``
or ``{"root_cause", "rationale", "patch": "<unified diff>"}`` (optionally a
``note``); its shape is validated strictly so that a typo in a fixture is an
error rather than a silently empty round.

Its :class:`ProviderInfo` says ``real_llm=False``: results obtained with it
demonstrate the *pipeline*, never model performance.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from leanc.core.errors import ProviderError, WorkspaceError
from leanc.core.models import FailureReport, ProviderInfo, VerificationResult
from leanc.core.workspace import normalize_relpath
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
from leanc.llm.prompts import render_failure
from leanc.specs.model import Spec

MAX_FIXTURE_BYTES = 4 * 1024 * 1024
MOCK_DISCLAIMER = "mock provider: replayed fixture, not LLM output"


class MockProvider(LLMProvider):
    def __init__(self, fixture_dir: Path):
        self.fixture_dir = Path(fixture_dir)
        self.info = ProviderInfo(name="mock", model=None, real_llm=False)

    # ------------------------------------------------------------------ fixture access
    def _path(self, rel: str) -> Path:
        """Resolve a fixture path, refusing traversal and symlink escapes."""
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

    def _read(self, rel: str) -> str | None:
        """Fixture text, or None if the fixture does not exist."""
        path = self._path(rel)
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

    # ------------------------------------------------------------------ provider API
    def generate_spec_draft(self, req: SpecDraftRequest) -> SpecDraft:
        text = self._read("spec_draft.yaml")
        if text is None:
            raise ProviderError(
                f"mock: no spec draft fixture at {self._where('spec_draft.yaml')} "
                "(the mock provider only replays fixtures; use --provider openai for a real model)"
            )
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ProviderError(f"mock: spec_draft.yaml is not valid YAML: {exc}") from None
        if not isinstance(data, dict):
            raise ProviderError("mock: spec_draft.yaml must contain a YAML mapping")
        return SpecDraft(
            spec_yaml=text,
            harness_c=self._read("harness_draft.c"),
            tests_c=self._read("tests_draft.c"),
            notes=f"{MOCK_DISCLAIMER} ({self._where('spec_draft.yaml')}); requested spec_id {req.spec_id}",
            raw_response=text,
        )

    def generate_code(self, req: CodeGenRequest) -> GeneratedCode:
        files: dict[str, str] = {}
        missing: list[str] = []
        for target in req.targets:
            content = self._read(f"generate/{target}")
            if content is None:
                missing.append(target)
            else:
                files[normalize_relpath(target)] = content
        if missing:
            raise ProviderError(
                f"mock: no generation fixture for {missing} (expected {self._where('generate')}/<path>)"
            )
        return GeneratedCode(
            files=files,
            notes=f"{MOCK_DISCLAIMER} ({self._where('generate')})",
            raw_response=json.dumps({"files": files}, indent=2, ensure_ascii=False, sort_keys=True),
        )

    def propose_patch(self, req: RepairRequest) -> RepairProposal:
        rel = f"repair/round-{int(req.round)}.json"
        text = self._read(rel)
        if text is None:
            return RepairProposal(root_cause="", rationale="", note=f"mock: no fixture for round {req.round}")
        try:
            obj = json.loads(text)
        except ValueError as exc:
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
        text = self._read("explain.md")
        if text is not None:
            return text
        return deterministic_summary(result)


def deterministic_summary(result: VerificationResult) -> str:
    """Short, deterministic explanation built only from the tool results."""
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
            inputs = f" (inputs: {', '.join(f'{i.name}={i.value}' for i in f.inputs[:10])})" if f.inputs else ""
            lines.append(f"- [{f.tool}] {f.kind}: {f.description}{where}{inputs}")
    else:
        lines.append("No failures were reported.")
    if status == "VERIFIED_BOUNDED":
        lines.append(
            "VERIFIED_BOUNDED holds only within the approved harness, input bounds and unwind limit; it is not a proof."
        )
    return "\n".join(lines)


__all__ = ["MOCK_DISCLAIMER", "MockProvider", "deterministic_summary"]
