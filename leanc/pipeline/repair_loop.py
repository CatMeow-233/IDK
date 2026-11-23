from __future__ import annotations

import json
import logging
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from leanc.backends.counterexample import fingerprint
from leanc.backends.wp.compile_flags import unapplied_compile_flags
from leanc.foundation.errors import LeanCError, PatchError, ProviderError
from leanc.foundation.models import (
    FailureReport,
    PolicyDecision,
    PolicyViolation,
    RepairRound,
    StageResult,
    VerificationResult,
)
from leanc.foundation.redact import redact
from leanc.foundation.status import (
    DEFINITE_FAILURES,
    REPAIRABLE,
    Outcome,
    Status,
    is_success,
    rank,
)
from leanc.foundation.workspace import Workspace
from leanc.llm.base import RepairProposal, RepairRequest
from leanc.pipeline.controller import verify
from leanc.repair.patch import (
    candidate_hash,
    changed_paths,
    diff_stats,
    make_unified_diff,
    proposal_to_contents,
)
from leanc.repair.policy import check_candidate
from leanc.specs.loader import spec_to_yaml
from leanc.specs.model import Spec
from leanc.toolchain.context import VerifyOptions

log = logging.getLogger("leanc.repair")

PROOF_GATES = ("wp", "lean")
INCONCLUSIVE_FAILURE_KINDS = ("unwind", "vacuity")
CRLF = "\r\n"
LF = "\n"

_DEFINITE_OUTCOMES = (Outcome.PASS, Outcome.FAIL)
INTEGRITY_STAGE = "integrity"
_LINE_NUMBER_IN_FINGERPRINT_RE = re.compile(r"@(.*?):(?:\d+|\?):")


@dataclass
class RepairOutcome:
    status: Status
    final: VerificationResult
    rounds: list[RepairRound] = field(default_factory=list)
    written_back: list[str] = field(default_factory=list)
    message: str = ""


def _compare_key(failure: FailureReport) -> str:
    return _LINE_NUMBER_IN_FINGERPRINT_RE.sub(lambda m: f"@{m.group(1)}:", fingerprint(failure), count=1)


def _failure_counter(vr: VerificationResult) -> Counter:
    return Counter(_compare_key(f) for f in vr.all_failures())


def _diff_failures(before: VerificationResult, after: VerificationResult) -> tuple[list[str], list[str]]:
    counted_before, counted_after = _failure_counter(before), _failure_counter(after)
    fixed = sorted((counted_before - counted_after).elements())
    introduced = sorted((counted_after - counted_before).elements())
    return fixed, introduced


def _inconclusive_failure_counter(vr: VerificationResult) -> Counter:
    return Counter(_compare_key(f) for f in vr.all_failures() if f.kind in INCONCLUSIVE_FAILURE_KINDS)


def stage_regressions(before: VerificationResult, after: VerificationResult) -> list[str]:
    regressions: list[str] = []
    for stage_before in before.stages:
        if stage_before.stage == "spec" or not stage_before.required or stage_before.outcome not in _DEFINITE_OUTCOMES:
            continue
        stage_after = after.stage(stage_before.stage)
        if stage_after is None or stage_after.outcome not in _DEFINITE_OUTCOMES:
            after_outcome = stage_after.outcome.value if stage_after else "missing"
            regressions.append(f"{stage_before.stage}: {stage_before.outcome.value} -> {after_outcome}")
    introduced = _inconclusive_failure_counter(after) - _inconclusive_failure_counter(before)
    for key in introduced.elements():
        regressions.append(f"introduced inconclusive result {key}")
    return regressions


def _required_gates(vr: VerificationResult) -> list[StageResult]:
    return [s for s in vr.stages if s.required and s.stage != "spec"]


def all_gates_pass(vr: VerificationResult) -> bool:
    if vr.approval is not None and not vr.approval.approved:
        return False
    required = _required_gates(vr)
    return bool(required) and all(s.outcome is Outcome.PASS for s in required)


def _goal_reached(vr: VerificationResult) -> bool:
    return is_success(vr.status) or all_gates_pass(vr)


def _proof_open_to_code_edits(stage: StageResult) -> bool:
    return (
        stage.stage in PROOF_GATES
        and stage.outcome is Outcome.INCONCLUSIVE
        and not unapplied_compile_flags(stage.details)
    )


def proof_repairable(vr: VerificationResult) -> bool:
    if vr.status is not Status.UNKNOWN:
        return False
    still_open = [s for s in _required_gates(vr) if s.outcome is not Outcome.PASS]
    return bool(still_open) and all(_proof_open_to_code_edits(s) for s in still_open)


def _is_proof_improvement(before: VerificationResult, after: VerificationResult) -> bool:
    if not proof_repairable(after):
        return False
    fixed, introduced = _diff_failures(before, after)
    return bool(fixed) and not introduced


def _passes(vr: VerificationResult, stage: str) -> bool:
    result = vr.stage(stage)
    return result is not None and result.outcome is Outcome.PASS


def _stage_failure_counter(vr: VerificationResult, stages: frozenset[str]) -> Counter:
    return Counter(_compare_key(f) for s in vr.stages if s.stage in stages for f in s.failures)


def _gates_run_despite_integrity(vr: VerificationResult) -> frozenset[str]:
    return frozenset(s.stage for s in vr.stages if s.stage != INTEGRITY_STAGE and s.outcome is not Outcome.SKIPPED)


def _holds_ground(before: VerificationResult, after: VerificationResult, stages: frozenset[str]) -> bool:
    if any(_passes(before, stage) and not _passes(after, stage) for stage in stages):
        return False
    return not _stage_failure_counter(after, stages) - _stage_failure_counter(before, stages)


def _unblocks_integrity(before: VerificationResult, after: VerificationResult) -> bool:
    if _passes(before, INTEGRITY_STAGE) or not _passes(after, INTEGRITY_STAGE):
        return False
    return _holds_ground(before, after, _gates_run_despite_integrity(before))


def is_improvement(before: VerificationResult, after: VerificationResult) -> bool:
    if _goal_reached(after):
        return True
    if proof_repairable(before):
        return _is_proof_improvement(before, after)
    if after.status not in DEFINITE_FAILURES:
        return False
    if stage_regressions(before, after):
        return False
    if _unblocks_integrity(before, after):
        return True
    if rank(after.status) > rank(before.status):
        return True
    if rank(after.status) < rank(before.status):
        return False
    fixed, introduced = _diff_failures(before, after)
    return bool(fixed) and not introduced and sum(_failure_counter(after).values()) < sum(
        _failure_counter(before).values()
    )


def _history_entry(r: RepairRound) -> dict:
    return {
        "round": r.round,
        "root_cause": r.root_cause,
        "files_changed": r.files_changed,
        "policy_allowed": None if r.policy is None else r.policy.allowed,
        "policy_violations": [] if r.policy is None else [f"{v.rule}: {v.message}" for v in r.policy.violations],
        "status_after": None if r.status_after is None else r.status_after.value,
        "fixed": r.fixed_failures,
        "introduced": r.introduced_failures,
        "reverted": r.reverted,
        "note": r.note,
    }


def _unrepairable_outcome(initial: VerificationResult, max_rounds: int) -> RepairOutcome | None:
    if is_success(initial.status):
        return RepairOutcome(status=initial.status, final=initial, message="already verified; no repair needed")
    if all_gates_pass(initial):
        return RepairOutcome(
            status=initial.status,
            final=initial,
            message=(
                "every required gate passes; the spec has no formal verification gate, so the status is "
                f"{initial.status.value} (nothing to repair; add a formal gate to the spec for a verified result)"
            ),
        )
    if initial.status not in REPAIRABLE and not proof_repairable(initial):
        return RepairOutcome(
            status=initial.status,
            final=initial,
            message=(
                f"{initial.status.value} is not repairable by editing the code under test "
                "(it needs a human decision: approve the spec, adjust bounds in a new spec version, fix the toolchain, ...)"
            ),
        )
    if max_rounds <= 0:
        return RepairOutcome(
            status=initial.status, final=initial, message="repair disabled (max_rounds 0): status unchanged"
        )
    return None


@dataclass(frozen=True)
class _TrustedContext:
    headers: dict[str, str]
    harness: str | None
    tests: str | None
    protected_context: dict[str, str]


def _read_repair_inputs(spec: Spec, ws: Workspace) -> tuple[dict[str, str], _TrustedContext]:
    sources = {rel: ws.read_text(rel) for rel in spec.editable_paths() if ws.exists(rel)}
    headers = {h: ws.read_text(h) for h in spec.sources.headers if ws.exists(h)}
    harness_path = spec.verification.harness if spec.verification is not None else None
    harness = ws.read_text(harness_path) if harness_path and ws.exists(harness_path) else None
    driver = spec.tests.driver
    tests = ws.read_text(driver) if driver and ws.exists(driver) else None
    already_given = {*spec.sources.headers, driver, harness_path}
    protected_context = {p: ws.read_text(p) for p in spec.protected_paths() if p not in already_given and ws.exists(p)}
    return sources, _TrustedContext(headers, harness, tests, protected_context)


def _write_proposal(path: Path, proposal: RepairProposal) -> None:
    recorded = {
        "root_cause": proposal.root_cause,
        "rationale": proposal.rationale,
        "files": proposal.files,
        "patch": proposal.patch,
        "note": proposal.note,
    }
    path.write_text(redact(json.dumps(recorded, indent=2, ensure_ascii=False)), encoding="utf-8")


def _is_lf_patch_for_crlf_sources(patch: str | None, sources: dict[str, str]) -> bool:
    return bool(patch) and "\r" not in patch and any(CRLF in content for content in sources.values())


def _lf_view(sources: dict[str, str]) -> dict[str, str]:
    return {rel: content.replace(CRLF, LF) for rel, content in sources.items()}


def _restore_crlf_line_endings(candidate: dict[str, str], originals: dict[str, str]) -> None:
    for rel, content in list(candidate.items()):
        original = originals.get(rel)
        if original is not None and CRLF in original and CRLF not in content:
            candidate[rel] = content.replace(LF, CRLF)


def _candidate_from(proposal: RepairProposal, sources: dict[str, str]) -> dict[str, str]:
    base = _lf_view(sources) if _is_lf_patch_for_crlf_sources(proposal.patch, sources) else sources
    candidate = proposal_to_contents(proposal.files, proposal.patch, base)
    _restore_crlf_line_endings(candidate, sources)
    return candidate


def _patch_rejection(exc: PatchError) -> PolicyDecision:
    return PolicyDecision(allowed=False, violations=[PolicyViolation(rule="PATCH_INVALID", message=str(exc))])


def _record_candidate(rr: RepairRound, round_dir: Path, run_dir: Path, sources: dict[str, str], candidate: dict[str, str]) -> None:
    patch_file = round_dir / "patch.diff"
    patch_file.write_text(make_unified_diff(sources, candidate), encoding="utf-8")
    rr.patch_path = str(patch_file.relative_to(run_dir))
    rr.files_changed = changed_paths(sources, candidate)
    rr.lines_added, rr.lines_removed = diff_stats(sources, candidate)
    rr.candidate_hash = candidate_hash(candidate)


def _accepted_note(vr: VerificationResult) -> str:
    if is_success(vr.status):
        return "accepted (verified)"
    if all_gates_pass(vr):
        return "accepted (all required gates pass; no formal gate)"
    return "accepted (improvement; continuing)"


def _reverted_note(rr: RepairRound, vr: VerificationResult, regressions: list[str]) -> str:
    note = (
        f"reverted: {vr.status.value} is not an improvement over {rr.status_before.value}"
        if rr.status_before
        else "reverted"
    )
    if regressions:
        note += " (gate regression: " + "; ".join(regressions) + ")"
    return note


def _save_sources(directory: Path, sources: dict[str, str]) -> None:
    for rel, content in sources.items():
        dest = directory / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def _write_back(ws: Workspace, sources: dict[str, str], backup: Path) -> list[str]:
    written: list[str] = []
    for rel, content in sources.items():
        if ws.exists(rel):
            backup_file = backup / rel
            backup_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ws.resolve(rel, must_exist=True), backup_file)
        if not ws.exists(rel) or ws.read_text(rel) != content:
            ws.write_text(rel, content)
            written.append(rel)
    return written


class _RepairSession:
    def __init__(
        self,
        spec_path: Path,
        spec: Spec,
        project_root: Path,
        provider,
        initial: VerificationResult,
        sources: dict[str, str],
        trusted: _TrustedContext,
        run_dir: Path,
        options: VerifyOptions,
    ):
        self.spec_path = spec_path
        self.spec = spec
        self.project_root = project_root
        self.provider = provider
        self.trusted = trusted
        self.run_dir = run_dir
        self.options = options
        self.best_vr = initial
        self.best_sources = sources
        self.base_run_id = initial.run_id
        self.seen = {candidate_hash(sources)}
        self.spec_yaml = spec_to_yaml(spec)
        self.rounds: list[RepairRound] = []
        self.repair_dir = run_dir / "repair"
        self.repair_dir.mkdir(parents=True, exist_ok=True)

    def run(self, max_rounds: int) -> None:
        for number in range(1, max_rounds + 1):
            verified = self._round(number)
            if verified is not None and _goal_reached(verified):
                break

    def _request(self, number: int) -> RepairRequest:
        return RepairRequest(
            requirement=self.spec.requirement,
            spec=self.spec,
            spec_yaml=self.spec_yaml,
            round=number,
            sources=dict(self.best_sources),
            headers=self.trusted.headers,
            harness=self.trusted.harness,
            tests=self.trusted.tests,
            diagnostics=[d for s in self.best_vr.stages for d in s.diagnostics],
            failures=self.best_vr.all_failures(),
            history=[_history_entry(r) for r in self.rounds],
            protected_context=self.trusted.protected_context,
        )

    def _editable_only(self, candidate: dict[str, str]) -> dict[str, str]:
        return {rel: content for rel, content in candidate.items() if rel in self.spec.editable_paths()}

    def _round(self, number: int) -> VerificationResult | None:
        round_dir = self.repair_dir / f"round-{number}"
        round_dir.mkdir(parents=True, exist_ok=True)
        rr = RepairRound(round=number, provider=self.provider.info.name, status_before=self.best_vr.status)
        request = self._request(number)
        self.rounds.append(rr)
        candidate = self._candidate(rr, request, round_dir)
        if candidate is None:
            return None
        _record_candidate(rr, round_dir, self.run_dir, self.best_sources, candidate)
        if not self._admissible(rr, number, candidate):
            return None
        vr = verify(
            self.spec_path,
            project_root=self.project_root,
            options=self.options,
            run_dir=round_dir / "verify",
            run_id=f"{self.base_run_id}-r{number}",
            candidate=self._editable_only(candidate),
            spec=self.spec,
        )
        self._judge(rr, number, round_dir, vr, candidate)
        return vr

    def _candidate(self, rr: RepairRound, request: RepairRequest, round_dir: Path) -> dict[str, str] | None:
        try:
            proposal = self.provider.propose_patch(request)
        except ProviderError as exc:
            rr.note = f"provider error: {exc}"
            return None
        rr.root_cause = proposal.root_cause or ""
        rr.rationale = proposal.rationale or ""
        _write_proposal(round_dir / "proposal.json", proposal)
        if not proposal.files and not proposal.patch:
            rr.note = proposal.note or "provider returned no proposal"
            return None
        try:
            return _candidate_from(proposal, self.best_sources)
        except PatchError as exc:
            rr.note = f"patch could not be applied: {exc}"
            rr.policy = _patch_rejection(exc)
            return None

    def _admissible(self, rr: RepairRound, number: int, candidate: dict[str, str]) -> bool:
        decision = check_candidate(
            self.spec, self.best_sources, candidate, max_changed_lines=self.spec.repair.max_changed_lines
        )
        rr.policy = decision
        if not decision.allowed:
            rr.note = "rejected by repair policy: " + ", ".join(sorted({v.rule for v in decision.violations}))
            log.info("round %d rejected by policy: %s", number, rr.note)
            return False
        if rr.candidate_hash in self.seen:
            rr.note = "cycle detected: candidate identical to an earlier candidate; not re-verified"
            return False
        self.seen.add(rr.candidate_hash)
        return True

    def _judge(self, rr: RepairRound, number: int, round_dir: Path, vr: VerificationResult, candidate: dict[str, str]) -> None:
        rr.verification_path = str((round_dir / "verify" / "verification.json").relative_to(self.run_dir))
        rr.status_after = vr.status
        rr.fixed_failures, rr.introduced_failures = _diff_failures(self.best_vr, vr)
        regressions = stage_regressions(self.best_vr, vr) if not is_success(vr.status) else []
        if is_improvement(self.best_vr, vr):
            self.best_vr, self.best_sources = vr, self._editable_only(candidate)
            rr.note = _accepted_note(vr)
        else:
            rr.reverted = True
            rr.note = _reverted_note(rr, vr, regressions)
        log.info("round %d: %s -> %s (%s)", number, rr.status_before, vr.status, rr.note)

    def outcome(self, ws: Workspace, write_back: bool) -> RepairOutcome:
        if _goal_reached(self.best_vr):
            return self._goal_outcome(ws, write_back)
        _save_sources(self.run_dir / "best-candidate", self.best_sources)
        message = (
            f"no verified candidate after {len(self.rounds)} round(s); best status {self.best_vr.status.value}; "
            "workspace left unchanged"
        )
        return RepairOutcome(status=Status.REPAIR_EXHAUSTED, final=self.best_vr, rounds=self.rounds, message=message)

    def _goal_outcome(self, ws: Workspace, write_back: bool) -> RepairOutcome:
        verified_dir = self.run_dir / "verified-candidate"
        _save_sources(verified_dir, self.best_sources)
        written = _write_back(ws, self.best_sources, self.run_dir / "backup" / "before-writeback") if write_back else []
        message = (
            f"verified after {len(self.rounds)} repair round(s); verified sources saved in {verified_dir}"
            if is_success(self.best_vr.status)
            else f"all required gates pass after {len(self.rounds)} repair round(s), but the spec has no formal gate: "
            f"status remains {self.best_vr.status.value}; sources saved in {verified_dir}"
        )
        return RepairOutcome(
            status=self.best_vr.status, final=self.best_vr, rounds=self.rounds, written_back=written, message=message
        )


def repair_loop(
    spec_path: Path,
    spec: Spec,
    project_root: Path,
    provider,
    initial: VerificationResult,
    *,
    run_dir: Path,
    options: VerifyOptions | None = None,
    max_rounds: int | None = None,
    write_back: bool = True,
) -> RepairOutcome:
    options = options or VerifyOptions()
    max_rounds = spec.repair.max_rounds if max_rounds is None else max_rounds
    ws = Workspace(project_root)
    unrepairable = _unrepairable_outcome(initial, max_rounds)
    if unrepairable is not None:
        return unrepairable
    try:
        sources, trusted = _read_repair_inputs(spec, ws)
    except LeanCError as exc:
        return RepairOutcome(status=initial.status, final=initial, message=f"cannot start repair: {exc}")
    session = _RepairSession(spec_path, spec, project_root, provider, initial, sources, trusted, run_dir, options)
    session.run(max_rounds)
    return session.outcome(ws, write_back)
