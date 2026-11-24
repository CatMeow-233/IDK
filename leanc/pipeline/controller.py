from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from leanc.backends.analyzer import run_static_analysis
from leanc.backends.cbmc import run_cbmc
from leanc.backends.clang import run_compile
from leanc.backends.inductive import run_inductive
from leanc.backends.integrity import run_integrity
from leanc.backends.lean import run_lean
from leanc.backends.sanitizer import run_sanitizer_tests, sanitizer_plan
from leanc.backends.wp import run_wp
from leanc.foundation.errors import LeanCError, WorkspaceError
from leanc.foundation.inductive_scope import COVERED_EXECUTIONS, HARNESS_NARROWING, UNREACHED_STATES
from leanc.foundation.models import (
    ApprovalCheck,
    StageResult,
    VerificationResult,
    utcnow,
)
from leanc.foundation.status import Outcome, Status, outcome_to_status, worst
from leanc.foundation.workspace import Workspace, normalize_relpath
from leanc.pipeline.reason_codes import derive_reason_codes
from leanc.reports.json_report import atomic_write_text, write_json
from leanc.reports.markdown import render_verification_md
from leanc.specs.approval import check_approval
from leanc.specs.loader import load_spec
from leanc.specs.model import GATE_ORDER, INDUCTIVE_GATE, MANDATORY_CHECKS, Spec
from leanc.toolchain.context import (
    BackendContext,
    VerifyOptions,
    find_project_root,
    make_context,
    new_run_id,
    snapshot_hashes,
)
from leanc.toolchain.environment import environment_info

log = logging.getLogger("leanc.controller")

STANDARD_LIMITATIONS = [
    "Sanitizer results only cover the paths executed by the trusted test cases; untested paths may still be defective.",
    "CBMC results are bounded: they hold only for the approved harness, its input constraints, the unwind bound and "
    "the checked property classes, under CBMC's C semantics and memory model.",
    "Properties that are not encoded as assertions in the approved harness (or as enabled CBMC checks) are not verified.",
    "The correctness of the verdict depends on the human-approved specification and harness being adequate.",
]
WP_LIMITATION = (
    "Frama-C/WP results are deductive and not bounded by an unwind limit, but hold only for the approved "
    "ACSL contract, under WP's Typed memory model, Frama-C's x86_64 machine model, the Frama-C libc "
    "specifications (assumed, not proved) and the listed provers; partial correctness unless the spec "
    "requires termination."
)
LEAN_LIMITATION = (
    "Lean 4 proofs are about the trusted MODEL; the model is linked to the C code only by differential "
    "test vectors, so they never make the C verdict PROVED."
)
LEAK_SANITIZER_LIMITATION = (
    "LeakSanitizer reports only heap blocks that are unreachable at exit; memory still referenced from globals, "
    "statics or stale stack words is not reported"
)
TEST_PATHS_ONLY_LEAK_LIMITATION = (
    "memory leaks are checked only on the executed test paths (no CBMC memory-leak check reached a verdict)"
)
NO_LEAK_CHECK_LIMITATION = "no memory-leak check reached a verdict"
CBMC_MEMORY_MODEL_LIMITATION = (
    "CBMC does not check pointer alignment, strict aliasing, restrict, or overflows between sub-objects of one "
    "allocation"
)
LEAK_SANITIZER = "leak"
VERDICT_OUTCOMES = (Outcome.PASS, Outcome.FAIL)
CBMC_MEMORY_LEAK_CHECK = "memory-leak"
UNAPPROVED_LIMITATION = "the specification is not approved: no result in this report is a verification result"
PROVED_REASON = (
    "all required gates passed; Frama-C/WP discharged every proof obligation of the approved contract "
    "and every runtime-error guard"
)
VERIFIED_BOUNDED_REASON = "all required gates passed; CBMC found no violation within the approved bounds"
VERIFIED_INDUCTIVE_REASON = (
    "all required gates passed; CBMC found no violation within the approved bounds, and the inductive gate showed "
    f"that {COVERED_EXECUTIONS} and the mandatory check classes only ({', '.join(MANDATORY_CHECKS)}), with every "
    "contracted loop proved by induction (invariant, assigns, decreases), static objects starting arbitrary and "
    f"allocations assumed to succeed; {HARNESS_NARROWING}; not proved: functions that are not claimed, "
    f"{UNREACHED_STATES} and the extra verification.checks classes, for which only the bounded CBMC result holds; "
    "known gaps: a memcpy, memmove or memset that overflows into a neighbouring field of the same object, or that "
    "writes into a string literal, is not reported, and calls of CBMC library models such as strlen, strcpy, "
    "strncpy and strncat make the gate fail closed (incomplete, never a pass); this is not PROVED"
)
INDUCTIVE_LIMITATION = (
    "The inductive gate (VERIFIED_INDUCTIVE, never PROVED) holds only in this sense: "
    f"{COVERED_EXECUTIONS} and the mandatory check classes only ({', '.join(MANDATORY_CHECKS)}), with every "
    "contracted loop proved by induction; static objects start arbitrary and allocations are assumed to succeed. "
    f"Note: {HARNESS_NARROWING}. Not covered: functions that are not claimed, {UNREACHED_STATES} and the extra "
    "verification.checks classes, for which only the bounded CBMC result holds. Known gaps: a memcpy, memmove or "
    "memset that overflows into a neighbouring field of the same object, or that writes into a string literal, is "
    "not reported; calls of CBMC library models such as strlen make the gate fail closed."
)
NO_FORMAL_GATE_REASONS = [
    "no formal verification gate (cbmc or wp) is required by the spec; "
    "dynamic and static checks alone cannot establish VERIFIED_BOUNDED"
]
FORMAL_GATE_DID_NOT_PASS_REASON = "formal gate did not pass"

MANDATORY_GATE_AFTER_COMPILE = "integrity"
FORMAL_GATES = ("cbmc", INDUCTIVE_GATE, "wp", "lean")
GATES_ANALYSING_OR_EXECUTING_CANDIDATE_CODE = ("sanitizer", "cbmc", INDUCTIVE_GATE, "wp", "lean")
EXCLUDED_BY_GATES_OPTION = "excluded by --gates (the result cannot be VERIFIED_BOUNDED)"
PROTECTED_FILE_CHANGED = "a protected file changed during verification"
COMPILATION_FAILED = "compilation did not succeed"
INTEGRITY_FAILED = (
    "the integrity gate did not pass; code with forbidden constructs is never executed, model-checked or proved"
)
INTEGRITY_NOT_RUN = (
    "the integrity gate did not run (it runs after a passing compile gate); code it has not checked is never "
    "executed, model-checked or proved"
)

_GATE_RUNNERS: dict[str, Callable[[BackendContext], StageResult]] = {
    "compile": run_compile,
    "static_analysis": run_static_analysis,
    "sanitizer": run_sanitizer_tests,
    "cbmc": run_cbmc,
    INDUCTIVE_GATE: run_inductive,
    "integrity": run_integrity,
    "wp": run_wp,
    "lean": run_lean,
}


def _gate_runner(name: str) -> Callable[[BackendContext], StageResult]:
    if name not in _GATE_RUNNERS:
        raise ValueError(f"unknown gate {name!r}")
    return _GATE_RUNNERS[name]


def _skipped(stage: str, why: str, required: bool = True) -> StageResult:
    return StageResult(
        stage=stage,
        outcome=Outcome.SKIPPED,
        status=outcome_to_status(stage, Outcome.SKIPPED),
        summary=f"skipped: {why}",
        required=required,
    )


def _run_gate(name: str, ctx: BackendContext) -> StageResult:
    start = time.monotonic()
    runner = _gate_runner(name)
    try:
        result = runner(ctx)
    except LeanCError as exc:
        result = StageResult(
            stage=name, outcome=Outcome.ERROR, status=Status.TOOL_ERROR, summary=f"{name} failed: {exc}"
        )
    if result.status is None and result.outcome is not Outcome.PASS:
        result.status = outcome_to_status(name, result.outcome)
    if not result.duration_s:
        result.duration_s = round(time.monotonic() - start, 3)
    return result


def _recheck_protected(spec: Spec, ws: Workspace, ctx: BackendContext, when: str) -> StageResult | None:
    chk = check_approval(spec, ws, files_ws=Workspace(ctx.snapshot_dir))
    if chk.approved:
        return None
    return StageResult(
        stage="spec",
        outcome=Outcome.FAIL,
        status=Status.SPEC_UNAPPROVED,
        summary=f"approval no longer valid {when}: " + "; ".join(chk.reasons),
        details={"reasons": [f"{when}: {r}" for r in chk.reasons], "spec_hash": chk.spec_hash},
    )


def ensure_runs_dir(root: Path) -> Path:
    root = Path(root)
    for directory in (root / ".leanc", root / ".leanc" / "runs"):
        if directory.is_symlink():
            raise WorkspaceError(f"{directory} is a symbolic link; refusing to write run artefacts through it")
        directory.mkdir(exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise WorkspaceError(f"{directory} is not a real directory")
    return root / ".leanc" / "runs"


def _unapproved_reasons(stages: list[StageResult]) -> list[str]:
    reasons: list[str] = []
    for spec_stage in (s for s in stages if s.stage == "spec" and s.outcome is not Outcome.PASS):
        reasons.extend(spec_stage.details.get("reasons", []) or [spec_stage.summary])
    return reasons


def _required_stage_passed(stages: list[StageResult], name: str) -> bool:
    return any(s.stage == name and s.required and s.outcome is Outcome.PASS for s in stages)


def aggregate(stages: list[StageResult], approved: bool) -> tuple[Status, list[str]]:
    if not approved:
        return Status.SPEC_UNAPPROVED, _unapproved_reasons(stages)
    contributed: list[Status] = []
    reasons: list[str] = []
    for stage in stages:
        if stage.stage == "spec" or not stage.required or stage.outcome is Outcome.PASS:
            continue
        contributed.append(stage.status or outcome_to_status(stage.stage, stage.outcome) or Status.UNKNOWN)
        reasons.append(f"[{stage.stage}] {stage.outcome.value}: {stage.summary}")
    if contributed:
        return worst(contributed), reasons
    wp = next((s for s in stages if s.stage == "wp" and s.required), None)
    if wp is not None and wp.outcome is Outcome.PASS:
        return Status.PROVED, [PROVED_REASON]
    formal = next((s for s in stages if s.stage == "cbmc"), None)
    if formal is None:
        return Status.UNKNOWN, list(NO_FORMAL_GATE_REASONS)
    if formal.outcome is Outcome.PASS and _required_stage_passed(stages, INDUCTIVE_GATE):
        return Status.VERIFIED_INDUCTIVE, [VERIFIED_INDUCTIVE_REASON]
    if formal.outcome is Outcome.PASS:
        return Status.VERIFIED_BOUNDED, [VERIFIED_BOUNDED_REASON]
    return Status.UNKNOWN, [FORMAL_GATE_DID_NOT_PASS_REASON]


def _write_candidate_into_snapshot(spec: Spec, ctx: BackendContext, candidate: dict[str, str]) -> None:
    for rel, content in candidate.items():
        norm = normalize_relpath(rel)
        if norm not in spec.editable_paths():
            raise LeanCError(f"candidate file {rel} is not an editable source of the spec")
        dest = ctx.snapshot_dir / norm
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def _approval_stage(spec: Spec, approval: ApprovalCheck, duration_s: float) -> StageResult:
    return StageResult(
        stage="spec",
        outcome=Outcome.PASS if approval.approved else Outcome.FAIL,
        status=None if approval.approved else Status.SPEC_UNAPPROVED,
        summary=(
            f"spec {spec.spec_id} v{spec.version} approved by {approval.approved_by} at {approval.approved_at}"
            if approval.approved
            else "spec is not approved: " + "; ".join(approval.reasons)
        ),
        details={"reasons": approval.reasons, "spec_hash": approval.spec_hash},
        duration_s=duration_s,
    )


def _reject_unknown_gate_selection(spec: Spec, options: VerifyOptions) -> None:
    if options.gates is None:
        return
    unknown = set(options.gates) - set(spec.gates)
    if unknown:
        raise LeanCError(f"--gates names gates the spec does not require: {sorted(unknown)}")


def _revoked(approval: ApprovalCheck, tampered: StageResult) -> ApprovalCheck:
    return approval.model_copy(update={"approved": False, "reasons": tampered.details["reasons"]})


@dataclass
class _GateSequence:
    spec: Spec
    ws: Workspace
    ctx: BackendContext
    options: VerifyOptions
    approval: ApprovalCheck
    stages: list[StageResult]
    blocked_reason: str | None = None
    integrity_blocked_reason: str | None = INTEGRITY_NOT_RUN

    def run(self) -> None:
        for gate in sorted(self.spec.gates, key=GATE_ORDER.index):
            self._run_or_skip(gate)
        if self.approval.approved:
            self._recheck_approval("after the gates")

    def _run_or_skip(self, gate: str) -> None:
        if self.options.gates is not None and gate not in self.options.gates:
            self.stages.append(_skipped(gate, EXCLUDED_BY_GATES_OPTION))
            return
        if self.blocked_reason:
            self.stages.append(_skipped(gate, self.blocked_reason))
            return
        if self.integrity_blocked_reason and gate in GATES_ANALYSING_OR_EXECUTING_CANDIDATE_CODE:
            self.stages.append(_skipped(gate, self.integrity_blocked_reason))
            return
        if gate in FORMAL_GATES and self.approval.approved:
            if self._recheck_approval(f"before the {gate} gate"):
                self.blocked_reason = PROTECTED_FILE_CHANGED
                self.stages.append(_skipped(gate, self.blocked_reason))
                return
        result = _run_gate(gate, self.ctx)
        self.stages.append(result)
        log.info("gate %s: %s %s", gate, result.outcome.value, result.summary)
        if gate == "compile" and result.outcome is Outcome.PASS:
            self._run_mandatory_integrity_gate()
        if gate == "compile" and result.outcome is not Outcome.PASS:
            self.blocked_reason = COMPILATION_FAILED
        elif self.options.fail_fast and result.outcome is not Outcome.PASS:
            self.blocked_reason = f"--fail-fast after {gate} {result.outcome.value}"

    def _recheck_approval(self, when: str) -> bool:
        tampered = _recheck_protected(self.spec, self.ws, self.ctx, when)
        if tampered is None:
            return False
        self.stages.append(tampered)
        self.approval = _revoked(self.approval, tampered)
        return True

    def _run_mandatory_integrity_gate(self) -> None:
        integrity = _run_gate(MANDATORY_GATE_AFTER_COMPILE, self.ctx)
        integrity.required = True
        self.stages.append(integrity)
        self.integrity_blocked_reason = None if integrity.outcome is Outcome.PASS else INTEGRITY_FAILED
        log.info("gate integrity: %s %s", integrity.outcome.value, integrity.summary)
        if self.options.fail_fast and integrity.outcome is not Outcome.PASS:
            self.blocked_reason = f"--fail-fast after integrity {integrity.outcome.value}"


def _stage(stages: list[StageResult], name: str) -> StageResult | None:
    return next((s for s in stages if s.stage == name), None)


def _assumptions(spec: Spec, stages: list[StageResult], approval: ApprovalCheck) -> list[str]:
    assumptions = [f"approved spec {spec.spec_id} version {spec.version} (sha256 {approval.spec_hash})"]
    for formal_name in FORMAL_GATES:
        formal = _stage(stages, formal_name)
        if formal is not None:
            assumptions.extend(str(a) for a in formal.details.get("assumptions", []))
    sanitizer = _stage(stages, "sanitizer")
    if sanitizer is not None and sanitizer.outcome is Outcome.PASS:
        assumptions.append(
            f"runtime checks: {len(spec.tests.cases)} trusted test case(s) under "
            f"{', '.join(spec.tests.sanitizers)} sanitizer(s)"
        )
    return assumptions


def _reached_verdict(stages: list[StageResult], gate: str) -> bool:
    stage = _stage(stages, gate)
    return stage is not None and stage.outcome in VERDICT_OUTCOMES


def memory_limitations(spec: Spec, stages: list[StageResult]) -> list[str]:
    cbmc_decided = _reached_verdict(stages, "cbmc")
    leak_sanitizer_decided = (
        _reached_verdict(stages, "sanitizer") and LEAK_SANITIZER in sanitizer_plan(spec.tests.sanitizers).effective
    )
    cbmc_decided_leaks = cbmc_decided and CBMC_MEMORY_LEAK_CHECK in spec.effective_cbmc_checks()
    limitations = [LEAK_SANITIZER_LIMITATION] if leak_sanitizer_decided else []
    if not cbmc_decided_leaks:
        limitations.append(TEST_PATHS_ONLY_LEAK_LIMITATION if leak_sanitizer_decided else NO_LEAK_CHECK_LIMITATION)
    if cbmc_decided:
        limitations.append(CBMC_MEMORY_MODEL_LIMITATION)
    return limitations


def _limitations(spec: Spec, ctx: BackendContext, approval: ApprovalCheck, stages: list[StageResult]) -> list[str]:
    limitations = list(STANDARD_LIMITATIONS)
    if "wp" in spec.gates:
        limitations.append(WP_LIMITATION)
    if "lean" in spec.gates:
        limitations.append(LEAN_LIMITATION)
    if INDUCTIVE_GATE in spec.gates:
        limitations.append(INDUCTIVE_LIMITATION)
    limitations.extend(memory_limitations(spec, stages))
    if not ctx.sandbox.active:
        limitations.append(f"no sandbox available: {ctx.sandbox.note}")
    if not approval.approved:
        limitations.insert(0, UNAPPROVED_LIMITATION)
    return limitations


def verify(
    spec_path: str | Path,
    *,
    project_root: str | Path | None = None,
    options: VerifyOptions | None = None,
    run_dir: str | Path | None = None,
    run_id: str | None = None,
    candidate: dict[str, str] | None = None,
    spec: Spec | None = None,
) -> VerificationResult:
    options = options or VerifyOptions()
    spec_path = Path(spec_path)
    spec = spec or load_spec(spec_path)
    root = Path(project_root).resolve() if project_root else find_project_root(spec_path)
    run_id = run_id or new_run_id()
    run_dir = Path(run_dir) if run_dir else ensure_runs_dir(root) / run_id
    started = utcnow()

    ctx = make_context(spec, root, run_dir, options)
    if candidate:
        _write_candidate_into_snapshot(spec, ctx, candidate)

    approval_started = time.monotonic()
    ws = Workspace(root)
    approval = check_approval(spec, ws, files_ws=Workspace(ctx.snapshot_dir))
    stages = [_approval_stage(spec, approval, round(time.monotonic() - approval_started, 3))]

    _reject_unknown_gate_selection(spec, options)
    if approval.approved or options.check_unapproved:
        gates = _GateSequence(spec=spec, ws=ws, ctx=ctx, options=options, approval=approval, stages=stages)
        gates.run()
        approval = gates.approval

    status, reasons = aggregate(stages, approval.approved)
    result = VerificationResult(
        run_id=run_id,
        spec_id=spec.spec_id,
        spec_version=spec.version,
        spec_hash=approval.spec_hash,
        status=status,
        reasons=reasons,
        reason_codes=derive_reason_codes(stages, status),
        approval=approval,
        stages=stages,
        source_hashes=snapshot_hashes(ctx),
        environment=environment_info(ctx.sandbox, ctx.tools),
        assumptions=_assumptions(spec, stages, approval),
        limitations=_limitations(spec, ctx, approval, stages),
        started_at=started,
        finished_at=utcnow(),
    )
    write_verification_artifacts(result, run_dir)
    return result


def write_verification_artifacts(result: VerificationResult, run_dir: Path) -> None:
    write_json(result, Path(run_dir) / "verification.json")
    atomic_write_text(Path(run_dir) / "verification.md", render_verification_md(result))
