"""Provider-neutral request/response types and the abstract LLM provider; nothing a provider returns is trusted."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from leanc.foundation.models import Diagnostic, FailureReport, ProviderInfo, VerificationResult
from leanc.specs.model import Spec


@dataclass
class SpecDraftRequest:
    """Ask for a candidate spec for a natural-language requirement."""

    requirement: str
    spec_id: str
    title: str | None = None
    language: str = "c17"


@dataclass
class SpecDraft:
    """An UNTRUSTED spec draft (YAML plus optional harness/test C drafts) that is inert until a human approves it."""

    spec_yaml: str
    harness_c: str | None = None
    tests_c: str | None = None
    notes: str = ""
    raw_response: str = ""


@dataclass
class CodeGenRequest:
    """Implement the editable targets of an approved spec against its protected headers (path -> content)."""

    spec: Spec
    spec_yaml: str
    headers: dict[str, str]
    targets: list[str]


@dataclass
class GeneratedCode:
    """Candidate source files (rel path -> full content)."""

    files: dict[str, str]
    notes: str = ""
    raw_response: str = ""


@dataclass
class RepairRequest:
    """Everything the repair agent may read for one round; only ``sources`` may be changed."""

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
    """Full replacement ``files`` or a unified-diff ``patch``; neither means no change and ``note`` says why."""

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
    """Model provider: raises ProviderError for transport/auth/format problems and never exposes credentials."""

    info: ProviderInfo

    @abstractmethod
    def generate_spec_draft(self, req: SpecDraftRequest) -> SpecDraft:
        """Draft a candidate spec (+ optional harness/tests); UNTRUSTED."""

    @abstractmethod
    def generate_code(self, req: CodeGenRequest) -> GeneratedCode:
        """Implement the requested editable files of an approved spec."""

    @abstractmethod
    def analyze_counterexample(self, failure: FailureReport, sources: dict[str, str], spec: Spec) -> str:
        """Explain, in prose, why the given failure happens in ``sources``."""

    @abstractmethod
    def propose_patch(self, req: RepairRequest) -> RepairProposal:
        """Propose a minimal root-cause fix for the failures in ``req``."""

    @abstractmethod
    def explain_failure(self, result: VerificationResult) -> str:
        """Human-readable explanation of a verification result."""

    def __repr__(self) -> str:
        """Provider identity without credentials."""
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
