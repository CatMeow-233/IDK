from __future__ import annotations

import sys
from pathlib import Path

from leanc.foundation.errors import LeanCError
from leanc.foundation.status import Status, exit_code
from leanc.foundation.workspace import Workspace
from leanc.pipeline.commands.console import Console, setup_logging
from leanc.pipeline.commands.session import (
    explicit_root,
    make_provider,
    new_run,
    spec_and_root,
)
from leanc.pipeline.generation import DRAFT_ORIGIN, draft_spec
from leanc.specs.approval import (
    approve_spec,
    approved_snapshot,
    check_approval,
    protected_file_hashes,
)
from leanc.specs.loader import (
    diff_spec_dicts,
    load_spec,
    spec_canonical_dict,
    spec_hash,
    spec_to_yaml,
)
from leanc.specs.model import Spec

APPROVAL_CONFIRMATION = "approve"
SHOWN_SPEC_CHANGES = 50
SHOWN_HASH_CHARS = 16
AI_DRAFT_APPROVAL_WARNING = (
    "leanc: WARNING: this spec is an AI DRAFT (provenance.origin: ai-draft). Approving makes it, its harness "
    "and its tests the trusted verification standard."
)


def _read_requirement(args) -> str:
    if args.requirement_file:
        try:
            return Path(args.requirement_file).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise LeanCError(f"cannot read --requirement-file: {exc}") from None
        except UnicodeDecodeError as exc:
            raise LeanCError(f"--requirement-file is not valid UTF-8 (byte offset {exc.start})") from None
    if args.requirement:
        return args.requirement
    raise LeanCError("give --requirement TEXT or --requirement-file FILE")


def cmd_spec_draft(args) -> int:
    console = Console(args.json)
    root = explicit_root(args) or Path(".").resolve()
    requirement = _read_requirement(args)
    _, run_dir = new_run(root, "draft-")
    setup_logging(run_dir / "leanc.log", args.verbose)
    provider = make_provider(args, root, run_dir)
    written = draft_spec(requirement, args.id, root, provider, title=args.title, force=args.force)
    console.emit(
        {"status": Status.SPEC_UNAPPROVED.value, "trusted": False, "written": written, "provider": provider.info.model_dump()}
    )
    console.info("wrote an UNTRUSTED spec draft (it has no effect until approved):")
    for kind, rel in written.items():
        if kind != "validation":
            console.info(f"  {kind:<8} {root / rel}")
    console.info(f"schema validation: {written.get('validation')}")
    if not provider.info.real_llm:
        console.info("note: produced by the mock provider (fixed fixtures), not by a real LLM")
    return exit_code(Status.SPEC_UNAPPROVED)


def _tell_reviewer(message: str, **print_options) -> None:
    print(message, file=sys.stderr, **print_options)


def _show_review(spec: Spec, root: Path, files: dict[str, str]) -> None:
    _tell_reviewer(f"Approving spec {spec.spec_id} v{spec.version} (sha256 {spec_hash(spec)})")
    previous = approved_snapshot(root, spec.spec_id)
    if previous:
        changes = diff_spec_dicts(previous, spec_canonical_dict(spec))
        _tell_reviewer("changes since the last approved version:" if changes else "no field changes since last approval")
        for change in changes[:SHOWN_SPEC_CHANGES]:
            _tell_reviewer("  " + change)
    _tell_reviewer("protected files (these become trusted and hash-locked):")
    for path, digest in files.items():
        _tell_reviewer(f"  {path}  {digest[:SHOWN_HASH_CHARS]}")


def _reviewer_confirms() -> bool:
    _tell_reviewer(f"type '{APPROVAL_CONFIRMATION}' to confirm: ", end="", flush=True)
    return input().strip() == APPROVAL_CONFIRMATION


def cmd_spec_approve(args) -> int:
    console = Console(args.json)
    spec_path, root = spec_and_root(args)
    spec = load_spec(spec_path)
    ws = Workspace(root)
    rel = ws.relpath(spec_path)
    files = protected_file_hashes(spec, ws)
    if spec.provenance.origin == DRAFT_ORIGIN:
        _tell_reviewer(AI_DRAFT_APPROVAL_WARNING)
    if not args.yes:
        if not sys.stdin.isatty():
            raise LeanCError("refusing to approve non-interactively; review the spec and pass --yes")
        _show_review(spec, root, files)
        if not _reviewer_confirms():
            console.err("not approved")
            return exit_code(Status.SPEC_UNAPPROVED)
    record = approve_spec(spec, rel, ws, approved_by=args.by, note=args.note or "")
    console.emit({k: v for k, v in record.items() if k != "spec_snapshot"})
    console.info(
        f"approved {spec.spec_id} v{spec.version} by {record['approved_by']} "
        f"(sha256 {record['spec_hash'][:SHOWN_HASH_CHARS]}...)"
    )
    for path, digest in files.items():
        console.info(f"  locked {path}  {digest[:SHOWN_HASH_CHARS]}")
    return 0


def cmd_spec_status(args) -> int:
    console = Console(args.json)
    spec_path, root = spec_and_root(args)
    spec = load_spec(spec_path)
    check = check_approval(spec, Workspace(root))
    previous = approved_snapshot(root, spec.spec_id)
    changes = diff_spec_dicts(previous, spec_canonical_dict(spec)) if previous else []
    console.emit({**check.model_dump(mode="json"), "changes_since_approval": changes, "origin": spec.provenance.origin})
    console.info(f"spec {spec.spec_id} v{spec.version}  origin={spec.provenance.origin}")
    console.info(f"status: {'APPROVED' if check.approved else 'SPEC_UNAPPROVED'}")
    if check.approved:
        console.info(f"approved by {check.approved_by} at {check.approved_at}")
    for reason in check.reasons:
        console.info(f"  - {reason}")
    for change in changes[:SHOWN_SPEC_CHANGES]:
        console.info(f"  {change}")
    return 0 if check.approved else exit_code(Status.SPEC_UNAPPROVED)


def cmd_spec_show(args) -> int:
    console = Console(args.json)
    spec = load_spec(args.spec)
    console.emit({"spec_hash": spec_hash(spec), "spec": spec_canonical_dict(spec), "protected_files": spec.protected_paths()})
    console.info(f"# sha256 {spec_hash(spec)}")
    console.info(f"# protected files: {', '.join(spec.protected_paths())}")
    if spec.verification is not None:
        console.info(f"# effective CBMC checks: {', '.join(spec.effective_cbmc_checks())}")
    console.info(f"# gates: {', '.join(spec.gates)} (+ mandatory integrity gate after compile)")
    console.info(spec_to_yaml(spec))
    return 0
