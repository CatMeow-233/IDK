from __future__ import annotations

from pathlib import Path

from leanc import __version__
from leanc.foundation.errors import ProviderError
from leanc.foundation.models import RunRecord, VerificationResult
from leanc.foundation.status import REPAIRABLE, Status, is_success
from leanc.foundation.workspace import Workspace
from leanc.pipeline.commands.console import Console, setup_logging
from leanc.pipeline.commands.session import (
    GENERATED_UNVERIFIED_STATUS,
    GENERATION_SUCCEEDED_EXIT_CODE,
    finish_run,
    make_provider,
    new_run,
    spec_and_root,
    verify_options,
)
from leanc.pipeline.controller import verify
from leanc.pipeline.generation import (
    SPEC_UNAPPROVED_PREFIX,
    GenerationRejected,
    generate_sources,
)
from leanc.pipeline.repair_loop import proof_repairable, repair_loop
from leanc.specs.approval import check_approval
from leanc.specs.loader import load_spec
from leanc.specs.model import Spec


def _record(command: str, run_id: str, status: Status, spec_path: Path, spec: Spec, root: Path, **fields) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        command=command,
        leanc_version=__version__,
        status=status,
        spec_path=str(spec_path),
        spec_id=spec.spec_id,
        project_root=str(root),
        **fields,
    )


def _verify_sources(args, spec_path: Path, root: Path, run_id: str, run_dir: Path) -> VerificationResult:
    return verify(
        spec_path,
        project_root=root,
        options=verify_options(args, root),
        run_dir=run_dir / "verify",
        run_id=run_id,
    )


def cmd_generate(args) -> int:
    console = Console(args.json)
    spec_path, root = spec_and_root(args)
    spec = load_spec(spec_path)
    run_id, run_dir = new_run(root, "gen-")
    setup_logging(run_dir / "leanc.log", args.verbose)
    provider = make_provider(args, root, run_dir)
    rec = _record("generate", run_id, Status.UNKNOWN, spec_path, spec, root, provider=provider.info)
    try:
        generation = generate_sources(spec, root, provider, run_dir=run_dir, force=args.force)
    except GenerationRejected as exc:
        rec.status = Status.SPEC_UNAPPROVED if str(exc).startswith(SPEC_UNAPPROVED_PREFIX) else Status.TOOL_ERROR
        rec.messages.append(str(exc))
        return finish_run(rec, run_dir, console)
    except ProviderError as exc:
        rec.status = Status.TOOL_ERROR
        rec.messages.append(f"provider error: {exc}")
        return finish_run(rec, run_dir, console)
    rec.generation = generation
    rec.messages.append(
        f"generated {len(generation.files)} file(s): {', '.join(generation.files) or '(none)'}; "
        "generated code is unverified until `leanc verify` passes"
    )
    if generation.note:
        rec.messages.append(generation.note)
    rec.status = GENERATED_UNVERIFIED_STATUS
    return finish_run(rec, run_dir, console, exit_override=GENERATION_SUCCEEDED_EXIT_CODE)


def cmd_verify(args) -> int:
    console = Console(args.json)
    spec_path, root = spec_and_root(args)
    spec = load_spec(spec_path)
    run_id, run_dir = new_run(root, "verify-")
    setup_logging(run_dir / "leanc.log", args.verbose)
    vr = _verify_sources(args, spec_path, root, run_id, run_dir)
    rec = _record("verify", run_id, vr.status, spec_path, spec, root, verification=vr, initial_verification=None)
    return finish_run(rec, run_dir, console)


def _needs_repair_provider(initial: VerificationResult) -> bool:
    return not is_success(initial.status) and (initial.status in REPAIRABLE or proof_repairable(initial))


def _repair_and_record(
    args, rec: RunRecord, spec_path: Path, spec: Spec, root: Path, run_dir: Path, initial: VerificationResult
) -> None:
    provider = None
    if _needs_repair_provider(initial):
        try:
            provider = make_provider(args, root, run_dir)
        except ProviderError as exc:
            rec.status = Status.TOOL_ERROR
            rec.messages.append(
                f"repair not attempted: LLM provider unavailable: {exc} (verification status: {initial.status.value})"
            )
            return
        rec.provider = provider.info
    outcome = repair_loop(
        spec_path,
        spec,
        root,
        provider,
        initial,
        run_dir=run_dir,
        options=verify_options(args, root),
        max_rounds=args.max_rounds,
        write_back=not args.no_write,
    )
    rec.repair_rounds = outcome.rounds
    rec.verification = outcome.final
    rec.status = outcome.status
    rec.messages.append(outcome.message)
    if outcome.written_back:
        what = "verified" if is_success(outcome.status) else "repaired (all required gates pass, NOT formally verified)"
        rec.messages.append(f"wrote {what} sources back to the workspace: " + ", ".join(outcome.written_back))
    elif args.no_write and is_success(outcome.status):
        rec.messages.append(
            "--no-write: the verified candidate was NOT written back and the workspace is NOT verified; "
            f"the candidate is in {run_dir / 'verified-candidate'}"
        )


def cmd_repair(args) -> int:
    console = Console(args.json)
    spec_path, root = spec_and_root(args)
    spec = load_spec(spec_path)
    run_id, run_dir = new_run(root, "repair-")
    setup_logging(run_dir / "leanc.log", args.verbose)
    initial = _verify_sources(args, spec_path, root, run_id, run_dir)
    rec = _record(
        "repair", run_id, initial.status, spec_path, spec, root, initial_verification=initial, verification=initial
    )
    _repair_and_record(args, rec, spec_path, spec, root, run_dir, initial)
    return finish_run(rec, run_dir, console)


def _spec_is_approved(rec: RunRecord, spec: Spec, spec_path: Path, root: Path) -> bool:
    check = check_approval(spec, Workspace(root))
    if check.approved:
        return True
    rec.status = Status.SPEC_UNAPPROVED
    rec.messages.append("spec is not approved: " + "; ".join(check.reasons))
    rec.messages.append(f"review the spec, harness and tests, then: leanc spec approve {spec_path} --by <name>")
    return False


def _sources_to_generate(args, spec: Spec, root: Path) -> bool:
    ws = Workspace(root)
    return args.regenerate or any(not ws.exists(p) for p in spec.sources.editable)


def _generation_succeeded(args, rec: RunRecord, spec: Spec, root: Path, run_dir: Path) -> bool:
    try:
        provider = make_provider(args, root, run_dir)
        rec.provider = provider.info
        rec.generation = generate_sources(spec, root, provider, run_dir=run_dir, force=args.regenerate)
    except GenerationRejected as exc:
        rec.status = Status.TOOL_ERROR
        rec.messages.append(str(exc))
        return False
    except ProviderError as exc:
        rec.status = Status.TOOL_ERROR
        rec.messages.append(f"provider error during generation: {exc}")
        return False
    rec.messages.append(f"generated: {', '.join(rec.generation.files) or '(nothing)'}")
    if rec.generation.note:
        rec.messages.append(rec.generation.note)
    return True


def cmd_run(args) -> int:
    console = Console(args.json)
    spec_path, root = spec_and_root(args)
    spec = load_spec(spec_path)
    run_id, run_dir = new_run(root, "run-")
    setup_logging(run_dir / "leanc.log", args.verbose)
    rec = _record("run", run_id, Status.UNKNOWN, spec_path, spec, root)
    if not _spec_is_approved(rec, spec, spec_path, root):
        return finish_run(rec, run_dir, console)
    if _sources_to_generate(args, spec, root) and not _generation_succeeded(args, rec, spec, root, run_dir):
        return finish_run(rec, run_dir, console)
    initial = _verify_sources(args, spec_path, root, run_id, run_dir)
    rec.initial_verification = initial
    rec.verification = initial
    rec.status = initial.status
    if not is_success(initial.status) and not args.no_repair:
        _repair_and_record(args, rec, spec_path, spec, root, run_dir, initial)
    return finish_run(rec, run_dir, console)
