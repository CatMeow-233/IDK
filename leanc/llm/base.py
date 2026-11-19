"""Provider-neutral request/response types and the abstract LLM provider.

The rest of Lean C talks to language models only through :class:`LLMProvider`.
That keeps two things true:

* the pipeline never depends on a particular vendor SDK (the HTTP provider uses
  the de-facto OpenAI ``/chat/completions`` wire format, the mock provider
  reads fixtures), and
* the model is only ever a *proposer*.  Every method returns candidate text
  (a spec draft, source files, a patch, an explanation); nothing a provider
  returns is trusted.  Drafts need human approval, code and patches go through
  the policy checker and the independent verification tools.

The request objects are plain dataclasses (not pydantic models) because they
carry already-validated pydantic objects (:class:`~leanc.specs.model.Spec`,
:class:`~leanc.core.models.FailureReport`, ...) and are never serialised as a
whole; prompt builders in :mod:`leanc.llm.prompts` decide what is sent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # imported lazily: keeps ``leanc.llm.base`` cheap to import
    from leanc.core.models import Diagnostic, FailureReport, ProviderInfo, VerificationResult
    from leanc.specs.model import Spec


# ------------------------------------------------------------------ spec draft
@dataclass
class SpecDraftRequest:
    """Ask for a *candidate* spec for a natural-language requirement."""

    requirement: str
    spec_id: str
    title: str | None = None
    language: str = "c17"


@dataclass
class SpecDraft:
    """An UNTRUSTED spec draft.  ``spec_yaml`` is YAML text in the Lean C spec
    schema; harness and test driver drafts are optional C sources.  None of it
    has any effect until a human reviews and approves it."""

    spec_yaml: str
    harness_c: str | None = None
    tests_c: str | None = None
    notes: str = ""
    raw_response: str = ""


# ------------------------------------------------------------------ code generation
@dataclass
class CodeGenRequest:
    """Implement the editable ``targets`` of an *approved* spec.

    ``headers`` maps protected header paths to their (approved) content so the
    model sees the exact interface it must implement."""

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


# ------------------------------------------------------------------ repair
@dataclass
class RepairRequest:
    """Everything the repair agent may look at for one round.

    ``sources`` are the current editable files (the only ones it may change);
    ``headers``, ``harness`` and ``tests`` are protected context;
    ``diagnostics``/``failures`` come from the independent tools; ``history``
    summarises earlier rounds (what was tried, why it was rejected/reverted)."""

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


@dataclass
class RepairProposal:
    """A proposed repair: either full replacement ``files`` or a unified diff
    ``patch`` (a/ b/ prefixes).  A proposal with neither means "no change";
    ``note`` explains why (e.g. the mock has no fixture for this round)."""

    root_cause: str
    rationale: str
    files: dict[str, str] | None = None
    patch: str | None = None
    raw_response: str = ""
    note: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.files and not self.patch


# ------------------------------------------------------------------ provider
class LLMProvider(ABC):
    """Abstract model provider.

    Implementations must raise :class:`~leanc.core.errors.ProviderError` for
    transport, authentication or response-format problems, and must never put
    credentials into exceptions, logs or transcripts.
    """

    #: Identity recorded in run reports.  ``real_llm`` is False for the mock
    #: provider so its fixed output is never presented as model performance.
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

    def __repr__(self) -> str:  # never include credentials
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
