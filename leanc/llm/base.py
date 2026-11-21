from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from leanc.foundation.models import (
    Diagnostic,
    FailureReport,
    ProviderInfo,
    VerificationResult,
)
from leanc.specs.model import Spec


@dataclass
class SpecDraftRequest:
    requirement: str
    spec_id: str
    title: str | None = None
    language: str = "c17"


@dataclass
class SpecDraft:
    spec_yaml: str
    harness_c: str | None = None
    tests_c: str | None = None
    notes: str = ""
    raw_response: str = ""


@dataclass
class CodeGenRequest:
    spec: Spec
    spec_yaml: str
    headers: dict[str, str]
    targets: list[str]


@dataclass
class GeneratedCode:
    files: dict[str, str]
    notes: str = ""
    raw_response: str = ""


@dataclass
class RepairRequest:
    requirement: str
    spec: Spec
    spec_yaml: str
    round: int
    sources: dict[str, str]
    headers: dict[str, str]
    harness: str | None
    tests: str | None
    diagnostics: list[Diagnostic]
    failures: list[FailureReport]
    history: list[dict]
    protected_context: dict[str, str] = field(default_factory=dict)


@dataclass
class RepairProposal:
    root_cause: str
    rationale: str
    files: dict[str, str] | None = None
    patch: str | None = None
    raw_response: str = ""
    note: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.files and not self.patch


class LLMProvider(ABC):
    info: ProviderInfo

    @abstractmethod
    def generate_spec_draft(self, req: SpecDraftRequest) -> SpecDraft:
        ...

    @abstractmethod
    def generate_code(self, req: CodeGenRequest) -> GeneratedCode:
        ...

    @abstractmethod
    def analyze_counterexample(self, failure: FailureReport, sources: dict[str, str], spec: Spec) -> str:
        ...

    @abstractmethod
    def propose_patch(self, req: RepairRequest) -> RepairProposal:
        ...

    @abstractmethod
    def explain_failure(self, result: VerificationResult) -> str:
        ...

    def __repr__(self) -> str:
        info = getattr(self, "info", None)
        if info is None:
            return f"<{type(self).__name__}>"
        return f"<{type(self).__name__} name={info.name!r} model={info.model!r} real_llm={info.real_llm}>"


__all__ = [
    "CodeGenRequest",
    "GeneratedCode",
    "LLMProvider",
    "RepairProposal",
    "RepairRequest",
    "SpecDraft",
    "SpecDraftRequest",
]
