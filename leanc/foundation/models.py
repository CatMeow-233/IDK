from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from leanc.foundation.reasons import Reason
from leanc.foundation.status import Outcome, Status

Severity = Literal["error", "warning", "note", "info"]
RunRelativePath = str
Sha256ByRelPath = dict[str, str]
RawToolOutputExcerpt = str
StableFailureId = str


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
            s += f" in {self.function}" + ("" if "(" in self.function else "()")
        return s


class PathStep(_Model):
    location: Location | None = None
    message: str = ""


class Diagnostic(_Model):
    tool: str
    rule_id: str | None = None
    severity: Severity = "warning"
    message: str
    location: Location | None = None
    path: list[PathStep] = Field(default_factory=list)
    raw: RawToolOutputExcerpt | None = None


class ToolInvocation(_Model):
    tool: str
    argv: list[str]
    cwd: str
    exit_code: int | None = None
    signal: int | None = None
    timed_out: bool = False
    output_truncated: bool = False
    duration_s: float = 0.0
    sandbox: str = "none"
    stdout_path: RunRelativePath | None = None
    stderr_path: RunRelativePath | None = None
    note: str | None = None


class CounterexampleInput(_Model):
    name: str
    value: str
    type: str | None = None


class TraceStep(_Model):
    kind: str
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
    "unproved",
]


OwnershipLoss = Literal["lost-during-call", "unreachable-by-return", "unreachable-by-next-event", "not-localised"]
OWNERSHIP_TRACE_LABEL = (
    "memory ownership trace (from an instrumented replay of the untrusted program; evidence, not a verdict)"
)
OWNERSHIP_LOSS_TEXT: dict[str, str] = {
    "lost-during-call": (
        "it was allocated during the call to {function} and was already unreachable before {function} returned, "
        "so it was lost in {function} or in a function it called"
    ),
    "unreachable-by-return": "no reference to it remained by the time {function} was about to return",
    "unreachable-by-next-event": (
        "it was lost in {function} or in its caller before the first traced event after {function} returned{returned_to}"
    ),
    "not-localised": "no probe before the program exited found it unreachable, so the loss is not localised",
}


class MemoryOwnershipTrace(_Model):
    test_case: str
    allocation: Location
    released_at: list[Location] = Field(default_factory=list)
    loss: OwnershipLoss
    function: Location | None = None
    returned_to: Location | None = None

    def describe(self) -> str:
        function = (self.function.function or self.function.short()) if self.function else "?"
        returned_to = f" (to {self.returned_to.short()})" if self.returned_to else ""
        released = (
            "allocations from this site are released at " + ", ".join(loc.short() for loc in self.released_at)
            if self.released_at
            else "no traced run released an allocation from this site"
        )
        loss = OWNERSHIP_LOSS_TEXT[self.loss].format(function=function, returned_to=returned_to)
        return f"allocated at {self.allocation.short()}; {released}; in test case {self.test_case!r} {loss}"


class FailureReport(_Model):
    id: StableFailureId
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
    ownership_trace: MemoryOwnershipTrace | None = None


class StageResult(_Model):
    stage: str
    outcome: Outcome
    status: Status | None = None
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
    reasons: list[str] = Field(default_factory=list)
    protected_files: Sha256ByRelPath = Field(default_factory=dict)


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


class StageReasonCode(_Model):
    code: Reason
    location: Location | None = None
    detail: str = ""


class ReasonCode(_Model):
    code: Reason
    stage: str
    location: Location | None = None
    detail: str = ""


class VerificationResult(_Model):
    run_id: str
    spec_id: str | None = None
    spec_version: int | None = None
    spec_hash: str | None = None
    status: Status
    reasons: list[str] = Field(default_factory=list)
    reason_codes: list[ReasonCode] = Field(default_factory=list)
    approval: ApprovalCheck | None = None
    stages: list[StageResult] = Field(default_factory=list)
    source_hashes: dict[str, str] = Field(default_factory=dict)
    environment: EnvironmentInfo | None = None
    assumptions: list[str] = Field(default_factory=list)
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
    patch_path: RunRelativePath | None = None
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
    verification_path: RunRelativePath | None = None


class ProviderInfo(_Model):
    name: str
    model: str | None = None
    real_llm: bool


class GenerationRecord(_Model):
    provider: ProviderInfo
    files: Sha256ByRelPath = Field(default_factory=dict)
    note: str = ""


class RunRecord(_Model):
    run_id: str
    command: str
    leanc_version: str
    status: Status
    spec_path: str | None = None
    spec_id: str | None = None
    project_root: str
    provider: ProviderInfo | None = None
    generation: GenerationRecord | None = None
    verification: VerificationResult | None = None
    initial_verification: VerificationResult | None = None
    repair_rounds: list[RepairRound] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    started_at: str = Field(default_factory=utcnow)
    finished_at: str | None = None
    artifacts: dict[str, RunRelativePath] = Field(default_factory=dict)
