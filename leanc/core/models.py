"""Unified, serialisable result data structures shared by every Lean C module.

All backends return :class:`StageResult`; the controller aggregates them into a
:class:`VerificationResult`; workflows (generate / repair / run) produce a
:class:`RunRecord`.  Everything here is plain data (pydantic v2) so it can be
written to ``run.json`` and re-loaded by ``leanc report``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from leanc.core.status import Outcome, Status

Severity = Literal["error", "warning", "note", "info"]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class Location(_Model):
    file: str | None = None
    line: int | None = None
    column: int | None = None
    function: str | None = None

    def short(self) -> str:
        parts = [self.file or "?"]
        if self.line is not None:
            parts.append(str(self.line))
            if self.column is not None:
                parts.append(str(self.column))
        s = ":".join(parts)
        if self.function:
            s += f" in {self.function}()"
        return s


class PathStep(_Model):
    """One step of an analyzer bug path / code flow."""

    location: Location | None = None
    message: str = ""


class Diagnostic(_Model):
    """A single finding from any tool.

    ``tool`` identifies the *detecting* tool precisely, e.g. ``clang``,
    ``clang-static-analyzer``, ``asan``, ``ubsan``, ``lsan``, ``cbmc``,
    ``leanc-policy``, ``test-oracle``.
    """

    tool: str
    rule_id: str | None = None
    severity: Severity = "warning"
    message: str
    location: Location | None = None
    path: list[PathStep] = Field(default_factory=list)
    raw: str | None = None  # raw excerpt of tool output this was parsed from


class ToolInvocation(_Model):
    tool: str
    argv: list[str]
    cwd: str
    exit_code: int | None = None
    signal: int | None = None
    timed_out: bool = False
    output_truncated: bool = False
    duration_s: float = 0.0
    sandbox: str = "none"  # "bwrap" | "none"
    stdout_path: str | None = None  # relative to the run directory
    stderr_path: str | None = None
    note: str | None = None


class CounterexampleInput(_Model):
    name: str
    value: str
    type: str | None = None


class TraceStep(_Model):
    kind: str  # assignment | function-call | function-return | failure | assume | location | output
    location: Location | None = None
    text: str = ""


FailureKind = Literal[
    "compile",
    "static",
    "runtime",
    "counterexample",
    "unwind",
    "vacuity",
    "policy",
    "timeout",
    "tool",
    "test-oracle",
]


class FailureReport(_Model):
    """Normalised description of one failure, suitable for humans and for the
    repair agent.  ``raw_ref`` always points at the unmodified tool output."""

    id: str  # stable identifier, e.g. "cbmc:get.pointer_dereference.11"
    kind: FailureKind
    tool: str
    property_id: str | None = None
    description: str
    location: Location | None = None
    inputs: list[CounterexampleInput] = Field(default_factory=list)
    call_stack: list[str] = Field(default_factory=list)
    trace: list[TraceStep] = Field(default_factory=list)
    explanation: str = ""
    raw_ref: str | None = None


class StageResult(_Model):
    stage: str  # spec | compile | static_analysis | sanitizer | cbmc
    outcome: Outcome
    status: Status | None = None  # global status this stage contributes (None if PASS)
    summary: str = ""
    required: bool = True
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    failures: list[FailureReport] = Field(default_factory=list)
    invocations: list[ToolInvocation] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    started_at: str = Field(default_factory=utcnow)
    duration_s: float = 0.0


class ApprovalCheck(_Model):
    approved: bool
    spec_id: str | None = None
    spec_version: int | None = None
    spec_hash: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    signature_valid: bool = False
    reasons: list[str] = Field(default_factory=list)  # why NOT approved (empty if approved)
    protected_files: dict[str, str] = Field(default_factory=dict)  # rel path -> sha256 (as approved)


class ToolInfo(_Model):
    name: str
    path: str | None = None
    version: str | None = None
    available: bool = False
    note: str | None = None


class EnvironmentInfo(_Model):
    leanc_version: str
    python: str
    platform: str
    tools: list[ToolInfo] = Field(default_factory=list)
    sandbox: str = "none"
    sandbox_note: str | None = None


class VerificationResult(_Model):
    run_id: str
    spec_id: str | None = None
    spec_version: int | None = None
    spec_hash: str | None = None
    status: Status
    reasons: list[str] = Field(default_factory=list)
    approval: ApprovalCheck | None = None
    stages: list[StageResult] = Field(default_factory=list)
    source_hashes: dict[str, str] = Field(default_factory=dict)
    environment: EnvironmentInfo | None = None
    assumptions: list[str] = Field(default_factory=list)  # scope of a bounded result
    limitations: list[str] = Field(default_factory=list)
    started_at: str = Field(default_factory=utcnow)
    finished_at: str | None = None

    def stage(self, name: str) -> StageResult | None:
        for s in self.stages:
            if s.stage == name:
                return s
        return None

    def all_failures(self) -> list[FailureReport]:
        out: list[FailureReport] = []
        for s in self.stages:
            out.extend(s.failures)
        return out


class PolicyViolation(_Model):
    rule: str
    message: str
    file: str | None = None
    line: int | None = None


class PolicyDecision(_Model):
    allowed: bool
    violations: list[PolicyViolation] = Field(default_factory=list)


class RepairRound(_Model):
    round: int
    provider: str
    root_cause: str = ""
    rationale: str = ""
    patch_path: str | None = None  # relative to run dir
    files_changed: list[str] = Field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0
    candidate_hash: str | None = None
    policy: PolicyDecision | None = None
    status_before: Status | None = None
    status_after: Status | None = None
    fixed_failures: list[str] = Field(default_factory=list)
    introduced_failures: list[str] = Field(default_factory=list)
    reverted: bool = False
    note: str = ""
    verification_path: str | None = None  # relative path of that round's verification json


class ProviderInfo(_Model):
    name: str
    model: str | None = None
    real_llm: bool  # False for the mock provider: its output is NOT LLM performance


class GenerationRecord(_Model):
    provider: ProviderInfo
    files: dict[str, str] = Field(default_factory=dict)  # rel path -> sha256 of generated content
    note: str = ""


class RunRecord(_Model):
    run_id: str
    command: str
    leanc_version: str
    status: Status  # final workflow status
    spec_path: str | None = None
    spec_id: str | None = None
    project_root: str
    provider: ProviderInfo | None = None
    generation: GenerationRecord | None = None
    verification: VerificationResult | None = None  # final verification
    initial_verification: VerificationResult | None = None
    repair_rounds: list[RepairRound] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    started_at: str = Field(default_factory=utcnow)
    finished_at: str | None = None
    artifacts: dict[str, str] = Field(default_factory=dict)  # name -> path relative to run dir
