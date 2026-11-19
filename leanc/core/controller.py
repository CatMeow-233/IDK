"""Verification Controller.

Runs the gates of an approved spec in order::

    spec approval check -> compile -> static analysis -> sanitizer tests -> CBMC

and combines their outcomes into one honest :class:`VerificationResult`.

Aggregation rules (see :mod:`leanc.core.status`):

* An unapproved / modified spec yields ``SPEC_UNAPPROVED`` and nothing else is
  claimed (gates are only run for information with ``check_unapproved``).
* A compile failure skips the remaining gates.
* The overall status is the worst contributed status; definite defects
  dominate inconclusive results.
* ``VERIFIED_BOUNDED`` requires that every required gate ran and passed *and*
  that the formal gate (CBMC) is among them.  Without a formal gate the best
  possible result is ``UNKNOWN``.  ``PROVED`` is never produced by V1.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from leanc.backends.tools import environment_info
from leanc.core.context import (
    BackendContext,
    VerifyOptions,
    find_project_root,
    make_context,
    new_run_id,
    runs_dir,
    snapshot_hashes,
)
from leanc.core.errors import LeanCError
from leanc.core.models import StageResult, VerificationResult, utcnow
from leanc.core.status import Outcome, Status, outcome_to_status, worst
from leanc.core.workspace import Workspace, normalize_relpath
from leanc.specs.approval import check_approval
from leanc.specs.loader import load_spec
from leanc.specs.model import Spec

log = logging.getLogger("leanc.controller")

STANDARD_LIMITATIONS = [
    "Sanitizer results only cover the paths executed by the trusted test cases; untested paths may still be defective.",
    "CBMC results are bounded: they hold only for the approved harness, its input constraints, the unwind bound and "
    "the checked property classes, under CBMC's C semantics and memory model.",
    "Properties that are not encoded as assertions in the approved harness (or as enabled CBMC checks) are not verified.",
    "The correctness of the verdict depends on the human-approved specification and harness being adequate.",
]


def _gate_runner(name: str) -> Callable[[BackendContext], StageResult]:
    # Imported lazily so that a broken / missing backend surfaces as TOOL_ERROR
    # for that gate instead of breaking the whole controller.
    if name == "compile":
        from leanc.backends.clang import run_compile

        return run_compile
    if name == "static_analysis":
        from leanc.backends.analyzer import run_static_analysis

        return run_static_analysis
    if name == "sanitizer":
        from leanc.backends.sanitizer import run_sanitizer_tests

        return run_sanitizer_tests
    if name == "cbmc":
        from leanc.backends.cbmc import run_cbmc

        return run_cbmc
    raise ValueError(f"unknown gate {name!r}")


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
    try:
        runner = _gate_runner(name)
        res = runner(ctx)
    except LeanCError as exc:
        res = StageResult(
            stage=name, outcome=Outcome.ERROR, status=Status.TOOL_ERROR, summary=f"{name} failed: {exc}"
        )
    except Exception as exc:  # a backend bug must never look like success
        log.exception("internal error in gate %s", name)
        res = StageResult(
            stage=name,
            outcome=Outcome.ERROR,
            status=Status.TOOL_ERROR,
            summary=f"internal error in {name} backend: {type(exc).__name__}: {exc}",
        )
    if res.status is None and res.outcome is not Outcome.PASS:
        res.status = outcome_to_status(name, res.outcome)
    if not res.duration_s:
        res.duration_s = round(time.monotonic() - start, 3)
    return res


def aggregate(stages: list[StageResult], approved: bool) -> tuple[Status, list[str]]:
    """Combine stage results into the overall status and the reasons for it."""
    reasons: list[str] = []
    if not approved:
        spec_stage = next((s for s in stages if s.stage == "spec"), None)
        if spec_stage:
            reasons.extend(spec_stage.details.get("reasons", []) or [spec_stage.summary])
        return Status.SPEC_UNAPPROVED, reasons
    contributed: list[Status] = []
    for s in stages:
        if s.stage == "spec" or not s.required:
            continue
        if s.outcome is Outcome.PASS:
            continue
        st = s.status or outcome_to_status(s.stage, s.outcome) or Status.UNKNOWN
        contributed.append(st)
        reasons.append(f"[{s.stage}] {s.outcome.value}: {s.summary}")
    if contributed:
        return worst(contributed), reasons
    formal = next((s for s in stages if s.stage == "cbmc"), None)
    if formal is None:
        return Status.UNKNOWN, ["no formal verification gate (cbmc) is required by the spec; "
                                "dynamic and static checks alone cannot establish VERIFIED_BOUNDED"]
    if formal.outcome is Outcome.PASS:
        return Status.VERIFIED_BOUNDED, ["all required gates passed; CBMC found no violation within the approved bounds"]
    return Status.UNKNOWN, ["formal gate did not pass"]  # pragma: no cover - covered by contributed


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
    """Verify the project's editable sources (or ``candidate`` contents for
    them) against the approved spec at ``spec_path``.

    ``candidate`` maps editable relative paths to new contents; it is used by
    the repair loop to verify a proposal *without* touching the workspace.
    """
    options = options or VerifyOptions()
    spec_path = Path(spec_path)
    spec = spec or load_spec(spec_path)
    root = Path(project_root).resolve() if project_root else find_project_root(spec_path)
    run_id = run_id or new_run_id()
    run_dir = Path(run_dir) if run_dir else runs_dir(root) / run_id
    started = utcnow()

    ctx = make_context(spec, root, run_dir, options)
    if candidate:
        for rel, content in candidate.items():
            norm = normalize_relpath(rel)
            if norm not in spec.sources.editable:
                raise LeanCError(f"candidate file {rel} is not an editable source of the spec")
            dest = ctx.snapshot_dir / norm
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

    # ---------------------------------------------------------- approval gate
    t0 = time.monotonic()
    ws = Workspace(root)
    approval = check_approval(spec, ws, files_ws=Workspace(ctx.snapshot_dir))
    spec_stage = StageResult(
        stage="spec",
        outcome=Outcome.PASS if approval.approved else Outcome.FAIL,
        status=None if approval.approved else Status.SPEC_UNAPPROVED,
        summary=(
            f"spec {spec.spec_id} v{spec.version} approved by {approval.approved_by} at {approval.approved_at}"
            if approval.approved
            else "spec is not approved: " + "; ".join(approval.reasons)
        ),
        details={"reasons": approval.reasons, "spec_hash": approval.spec_hash},
        duration_s=round(time.monotonic() - t0, 3),
    )
    stages: list[StageResult] = [spec_stage]

    gates = list(spec.gates)
    if options.gates is not None:
        unknown = set(options.gates) - set(spec.gates)
        if unknown:
            raise LeanCError(f"--gates names gates the spec does not require: {sorted(unknown)}")
    run_gates = approval.approved or options.check_unapproved
    if run_gates:
        blocked_reason: str | None = None
        for g in gates:
            if options.gates is not None and g not in options.gates:
                stages.append(_skipped(g, "excluded by --gates (the result cannot be VERIFIED_BOUNDED)"))
                continue
            if blocked_reason:
                stages.append(_skipped(g, blocked_reason))
                continue
            res = _run_gate(g, ctx)
            stages.append(res)
            log.info("gate %s: %s %s", g, res.outcome.value, res.summary)
            if g == "compile" and res.outcome is not Outcome.PASS:
                blocked_reason = "compilation did not succeed"
            elif options.fail_fast and res.outcome is not Outcome.PASS:
                blocked_reason = f"--fail-fast after {g} {res.outcome.value}"

    status, reasons = aggregate(stages, approval.approved)

    assumptions = [
        f"approved spec {spec.spec_id} version {spec.version} (sha256 {approval.spec_hash})",
    ]
    cbmc_stage = next((s for s in stages if s.stage == "cbmc"), None)
    if cbmc_stage is not None:
        assumptions.extend(str(a) for a in cbmc_stage.details.get("assumptions", []))
    san_stage = next((s for s in stages if s.stage == "sanitizer"), None)
    if san_stage is not None and san_stage.outcome is Outcome.PASS:
        assumptions.append(
            f"runtime checks: {len(spec.tests.cases)} trusted test case(s) under "
            f"{', '.join(spec.tests.sanitizers)} sanitizer(s)"
        )
    limitations = list(STANDARD_LIMITATIONS)
    if not ctx.sandbox.active:
        limitations.append(f"no sandbox available: {ctx.sandbox.note}")
    if not approval.approved:
        limitations.insert(0, "the specification is not approved: no result in this report is a verification result")

    result = VerificationResult(
        run_id=run_id,
        spec_id=spec.spec_id,
        spec_version=spec.version,
        spec_hash=approval.spec_hash,
        status=status,
        reasons=reasons,
        approval=approval,
        stages=stages,
        source_hashes=snapshot_hashes(ctx),
        environment=environment_info(ctx.sandbox, ctx.tools),
        assumptions=assumptions,
        limitations=limitations,
        started_at=started,
        finished_at=utcnow(),
    )
    write_verification_artifacts(result, run_dir)
    return result


def write_verification_artifacts(result: VerificationResult, run_dir: Path) -> None:
    from leanc.reports.json_report import write_json
    from leanc.reports.markdown import render_verification_md

    write_json(result, Path(run_dir) / "verification.json")
    (Path(run_dir) / "verification.md").write_text(render_verification_md(result), encoding="utf-8")
