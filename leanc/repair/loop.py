"""Counterexample-driven repair loop (Repair Agent orchestration).

Each round:

1. build a :class:`~leanc.llm.base.RepairRequest` from the approved spec, the
   current best sources, all failure reports (compiler, analyzer, sanitizer,
   CBMC counterexamples) and the history of earlier rounds;
2. ask the provider for a proposal (full files or a unified diff);
3. turn it into a candidate, compute the patch and **policy-check** it
   (protected files, CBMC built-ins, assumption injection, assertion deletion,
   warning/sanitizer suppression, trivialised target, size limit, ...);
4. reject candidates already seen (cycle detection);
5. verify the candidate *in a fresh snapshot* with the full required gates
   (the workspace is untouched while rounds run);
6. compare failures before/after (fixed / introduced) and keep the candidate
   only if it is a genuine improvement; otherwise revert to the best one.

The loop stops on a success status or after ``max_rounds`` rounds, in which
case the result is ``REPAIR_EXHAUSTED``.  Only a candidate that reached a
success status is written back to the workspace.  Inconclusive results
(UNKNOWN / TIMEOUT / TOOL_ERROR) never count as improvements: making code
unverifiable is not a repair.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from leanc.core.context import VerifyOptions
from leanc.core.errors import LeanCError, PatchError, ProviderError
from leanc.core.models import (
    FailureReport,
    PolicyDecision,
    PolicyViolation,
    RepairRound,
    VerificationResult,
)
from leanc.core.redact import redact
from leanc.core.status import DEFINITE_FAILURES, REPAIRABLE, Outcome, Status, is_success, rank
from leanc.core.workspace import Workspace
from leanc.specs.loader import spec_to_yaml
from leanc.specs.model import Spec

log = logging.getLogger("leanc.repair")


@dataclass
class RepairOutcome:
    status: Status  # final workflow status (success status, REPAIR_EXHAUSTED, or the unrepairable status)
    final: VerificationResult
    rounds: list[RepairRound] = field(default_factory=list)
    written_back: list[str] = field(default_factory=list)
    message: str = ""


_LINE_RE = re.compile(r"@(.*?):(?:\d+|\?):")


def _display_fp(f: FailureReport) -> str:
    from leanc.repair.counterexample import fingerprint

    try:
        return fingerprint(f)
    except Exception:  # fingerprint is best-effort for unusual reports
        return f"{f.kind}:{f.tool}:{f.property_id}:{(f.location.short() if f.location else '')}"


def _compare_key(f: FailureReport) -> str:
    """Line-insensitive identity used to decide fixed / introduced failures:
    a genuine fix that merely shifts the remaining defects to other line
    numbers must not look like it introduced new ones."""
    return _LINE_RE.sub(lambda m: f"@{m.group(1)}:", _display_fp(f), count=1)


def _failure_counter(vr: VerificationResult) -> Counter:
    return Counter(_compare_key(f) for f in vr.all_failures())


def _diff_failures(before: VerificationResult, after: VerificationResult) -> tuple[list[str], list[str]]:
    """(fixed, introduced) as multisets of line-insensitive failure keys."""
    cb, ca = _failure_counter(before), _failure_counter(after)
    fixed = sorted((cb - ca).elements())
    introduced = sorted((ca - cb).elements())
    return fixed, introduced


_DEFINITE_OUTCOMES = (Outcome.PASS, Outcome.FAIL)


def stage_regressions(before: VerificationResult, after: VerificationResult) -> list[str]:
    """Required gates that reached a definite verdict (PASS / FAIL) before but
    not after (INCONCLUSIVE / TIMEOUT / ERROR / SKIPPED / missing), plus newly
    introduced unwind / vacuity failures.  Any regression disqualifies a
    candidate: hiding a counterexample by making a gate inconclusive is not
    a repair."""
    out: list[str] = []
    for sb in before.stages:
        if sb.stage == "spec" or not sb.required or sb.outcome not in _DEFINITE_OUTCOMES:
            continue
        sa = after.stage(sb.stage)
        if sa is None or sa.outcome not in _DEFINITE_OUTCOMES:
            out.append(f"{sb.stage}: {sb.outcome.value} -> {sa.outcome.value if sa else 'missing'}")
    kinds_before = Counter(_compare_key(f) for f in before.all_failures() if f.kind in ("unwind", "vacuity"))
    kinds_after = Counter(_compare_key(f) for f in after.all_failures() if f.kind in ("unwind", "vacuity"))
    for k in (kinds_after - kinds_before).elements():
        out.append(f"introduced inconclusive result {k}")
    return out


PROOF_GATES = ("wp", "lean")


def all_gates_pass(vr: VerificationResult) -> bool:
    """Every required gate passed.  For specs without a formal gate this is the
    best achievable outcome (status stays UNKNOWN: nothing was formally
    verified), so the repair loop treats it as its goal - without ever
    reporting it as a success status."""
    if vr.approval is not None and not vr.approval.approved:
        return False
    req = [s for s in vr.stages if s.required and s.stage != "spec"]
    return bool(req) and all(s.outcome is Outcome.PASS for s in req)


def _goal_reached(vr: VerificationResult) -> bool:
    return is_success(vr.status) or all_gates_pass(vr)


def proof_repairable(vr: VerificationResult) -> bool:
    """UNKNOWN only because proof obligations are not yet discharged (WP goals
    unproved, Lean theorems unproved) while every other required gate passed.
    The repair agent may then add *proof annotations* (loop invariants, helper
    contracts, Lean proofs); the code under test must keep passing everything."""
    if vr.status is not Status.UNKNOWN:
        return False
    open_ = [s for s in vr.stages if s.required and s.stage != "spec" and s.outcome is not Outcome.PASS]
    return bool(open_) and all(s.stage in PROOF_GATES and s.outcome is Outcome.INCONCLUSIVE for s in open_)


def is_improvement(before: VerificationResult, after: VerificationResult) -> bool:
    """A candidate is kept only if it verifies, or if it is still a *definite*
    failure that is strictly better (better-ranked definite status, or the
    same status with strictly fewer failures and no newly introduced ones),
    and no required gate lost its definite verdict."""
    if _goal_reached(after):
        return True
    if proof_repairable(before):
        # proof repair: strictly fewer open obligations, nothing else may change
        if not proof_repairable(after):
            return False
        fixed, introduced = _diff_failures(before, after)
        return bool(fixed) and not introduced
    if after.status not in DEFINITE_FAILURES:
        return False  # UNKNOWN / TIMEOUT / TOOL_ERROR / SPEC_UNAPPROVED are never improvements
    if stage_regressions(before, after):
        return False
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
    from leanc.core.controller import verify
    from leanc.llm.base import RepairRequest
    from leanc.repair.patch import (
        candidate_hash,
        changed_paths,
        diff_stats,
        make_unified_diff,
        proposal_to_contents,
    )
    from leanc.repair.policy import check_candidate

    options = options or VerifyOptions()
    max_rounds = spec.repair.max_rounds if max_rounds is None else max_rounds
    ws = Workspace(project_root)

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

    try:
        best_sources = {rel: ws.read_text(rel) for rel in spec.editable_paths() if ws.exists(rel)}
        headers = {h: ws.read_text(h) for h in spec.sources.headers if ws.exists(h)}
        hpath = spec.verification.harness if spec.verification is not None else None
        harness = ws.read_text(hpath) if hpath and ws.exists(hpath) else None
        tests = ws.read_text(spec.tests.driver) if spec.tests.driver and ws.exists(spec.tests.driver) else None
        known = {*spec.sources.headers, spec.tests.driver, hpath}
        protected_context = {
            p: ws.read_text(p) for p in spec.protected_paths() if p not in known and ws.exists(p)
        }
    except LeanCError as exc:
        return RepairOutcome(status=initial.status, final=initial, message=f"cannot start repair: {exc}")
    best_vr = initial
    base_run_id = initial.run_id
    seen = {candidate_hash(best_sources)}
    spec_yaml = spec_to_yaml(spec)
    rounds: list[RepairRound] = []
    repair_dir = run_dir / "repair"
    repair_dir.mkdir(parents=True, exist_ok=True)

    for n in range(1, max_rounds + 1):
        rdir = repair_dir / f"round-{n}"
        rdir.mkdir(parents=True, exist_ok=True)
        rr = RepairRound(round=n, provider=provider.info.name, status_before=best_vr.status)
        diagnostics = [d for s in best_vr.stages for d in s.diagnostics]
        req = RepairRequest(
            requirement=spec.requirement,
            spec=spec,
            spec_yaml=spec_yaml,
            round=n,
            sources=dict(best_sources),
            headers=headers,
            harness=harness,
            tests=tests,
            diagnostics=diagnostics,
            failures=best_vr.all_failures(),
            history=[_history_entry(r) for r in rounds],
            protected_context=protected_context,
        )
        try:
            proposal = provider.propose_patch(req)
        except ProviderError as exc:
            rr.note = f"provider error: {exc}"
            rounds.append(rr)
            continue
        rr.root_cause = proposal.root_cause or ""
        rr.rationale = proposal.rationale or ""
        (rdir / "proposal.json").write_text(
            redact(
                json.dumps(
                    {
                        "root_cause": proposal.root_cause,
                        "rationale": proposal.rationale,
                        "files": proposal.files,
                        "patch": proposal.patch,
                        "note": proposal.note,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            ),
            encoding="utf-8",
        )
        if not proposal.files and not proposal.patch:
            rr.note = proposal.note or "provider returned no proposal"
            rounds.append(rr)
            continue
        try:
            base = best_sources
            if proposal.patch and "\r" not in proposal.patch and any("\r\n" in v for v in best_sources.values()):
                # LF diff against CRLF sources: apply to the LF view, CRLF is restored below
                base = {k: v.replace("\r\n", "\n") for k, v in best_sources.items()}
            candidate = proposal_to_contents(proposal.files, proposal.patch, base)
            for rel, content in list(candidate.items()):
                # Preserve CRLF line endings of existing sources: models (and
                # the response parser) normalise to LF, which would otherwise
                # rewrite every line and trip the patch-size limit.
                old = best_sources.get(rel)
                if old is not None and "\r\n" in old and "\r\n" not in content:
                    candidate[rel] = content.replace("\n", "\r\n")
        except PatchError as exc:
            rr.note = f"patch could not be applied: {exc}"
            rr.policy = PolicyDecision(
                allowed=False, violations=[PolicyViolation(rule="PATCH_INVALID", message=str(exc))]
            )
            rounds.append(rr)
            continue
        diff = make_unified_diff(best_sources, candidate)
        (rdir / "patch.diff").write_text(diff, encoding="utf-8")
        rr.patch_path = str((rdir / "patch.diff").relative_to(run_dir))
        rr.files_changed = changed_paths(best_sources, candidate)
        rr.lines_added, rr.lines_removed = diff_stats(best_sources, candidate)
        rr.candidate_hash = candidate_hash(candidate)

        decision = check_candidate(spec, best_sources, candidate, max_changed_lines=spec.repair.max_changed_lines)
        rr.policy = decision
        if not decision.allowed:
            rr.note = "rejected by repair policy: " + ", ".join(sorted({v.rule for v in decision.violations}))
            rounds.append(rr)
            log.info("round %d rejected by policy: %s", n, rr.note)
            continue
        if rr.candidate_hash in seen:
            rr.note = "cycle detected: candidate identical to an earlier candidate; not re-verified"
            rounds.append(rr)
            continue
        seen.add(rr.candidate_hash)

        vr = verify(
            spec_path,
            project_root=project_root,
            options=options,
            run_dir=rdir / "verify",
            run_id=f"{base_run_id}-r{n}",
            candidate={k: v for k, v in candidate.items() if k in spec.editable_paths()},
            spec=spec,
        )
        rr.verification_path = str((rdir / "verify" / "verification.json").relative_to(run_dir))
        rr.status_after = vr.status
        rr.fixed_failures, rr.introduced_failures = _diff_failures(best_vr, vr)
        regressions = stage_regressions(best_vr, vr) if not is_success(vr.status) else []
        if is_improvement(best_vr, vr):
            best_vr, best_sources = vr, {k: v for k, v in candidate.items() if k in spec.editable_paths()}
            rr.note = "accepted" + (
                " (verified)"
                if is_success(vr.status)
                else " (all required gates pass; no formal gate)" if all_gates_pass(vr) else " (improvement; continuing)"
            )
        else:
            rr.reverted = True
            rr.note = (
                f"reverted: {vr.status.value} is not an improvement over {rr.status_before.value}"
                if rr.status_before
                else "reverted"
            )
            if regressions:
                rr.note += " (gate regression: " + "; ".join(regressions) + ")"
        rounds.append(rr)
        log.info("round %d: %s -> %s (%s)", n, rr.status_before, vr.status, rr.note)
        if _goal_reached(vr):
            break

    written: list[str] = []
    if _goal_reached(best_vr):
        final_status = best_vr.status  # UNKNOWN when the spec has no formal gate: never upgraded
        vdir = run_dir / "verified-candidate"
        for rel, content in best_sources.items():
            d = vdir / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_text(content, encoding="utf-8")
        if write_back:
            backup = run_dir / "backup" / "before-writeback"
            for rel, content in best_sources.items():
                if ws.exists(rel):
                    b = backup / rel
                    b.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ws.resolve(rel, must_exist=True), b)
                if not ws.exists(rel) or ws.read_text(rel) != content:
                    ws.write_text(rel, content)
                    written.append(rel)
        message = (
            f"verified after {len(rounds)} repair round(s); verified sources saved in {vdir}"
            if is_success(best_vr.status)
            else f"all required gates pass after {len(rounds)} repair round(s), but the spec has no formal gate: "
            f"status remains {best_vr.status.value}; sources saved in {vdir}"
        )
    else:
        final_status = Status.REPAIR_EXHAUSTED
        best_dir = run_dir / "best-candidate"
        for rel, content in best_sources.items():
            d = best_dir / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_text(content, encoding="utf-8")
        message = (
            f"no verified candidate after {len(rounds)} round(s); best status {best_vr.status.value}; "
            "workspace left unchanged"
        )
    return RepairOutcome(status=final_status, final=best_vr, rounds=rounds, written_back=written, message=message)
