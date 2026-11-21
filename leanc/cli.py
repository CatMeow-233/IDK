"""``leanc`` command-line interface.

Exit codes (see ``leanc --help``): 0 = VERIFIED_BOUNDED / PROVED, 10 COMPILE_ERROR,
11 STATIC_ANALYSIS_WARNING, 12 RUNTIME_TEST_FAILED, 13 COUNTEREXAMPLE_FOUND,
20 UNKNOWN, 21 TIMEOUT, 30 TOOL_ERROR, 40 SPEC_UNAPPROVED, 50 REPAIR_EXHAUSTED,
2 usage / input error, 70 internal error.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from leanc import __version__
from leanc.foundation.errors import LeanCError, ProviderError
from leanc.foundation.hashing import sha256_bytes
from leanc.foundation.models import RunRecord, utcnow
from leanc.foundation.status import EXIT_INTERNAL, EXIT_USAGE, REPAIRABLE, Status, exit_code, is_success
from leanc.foundation.workspace import Workspace
from leanc.llm.factory import get_provider
from leanc.pipeline.config import CONFIG_FILE, load_config
from leanc.pipeline.controller import ensure_runs_dir, verify
from leanc.pipeline.doctor import checks_to_json, run_doctor
from leanc.pipeline.generation import GenerationRejected, draft_spec, generate_sources
from leanc.pipeline.repair_loop import proof_repairable, repair_loop
from leanc.reports.json_report import read_run_record, summary, write_json
from leanc.reports.markdown import render_run_md
from leanc.specs.approval import approve_spec, approved_snapshot, check_approval, protected_file_hashes
from leanc.specs.loader import diff_spec_dicts, load_spec, spec_canonical_dict, spec_hash, spec_to_yaml
from leanc.toolchain.context import VerifyOptions, find_project_root, new_run_id, runs_dir

log = logging.getLogger("leanc")

EPILOG = """exit codes:
  0  VERIFIED_BOUNDED / PROVED     10 COMPILE_ERROR         11 STATIC_ANALYSIS_WARNING
  12 RUNTIME_TEST_FAILED           13 COUNTEREXAMPLE_FOUND  20 UNKNOWN
  21 TIMEOUT                       30 TOOL_ERROR            40 SPEC_UNAPPROVED
  50 REPAIR_EXHAUSTED              2  usage/input error     70 internal error

VERIFIED_BOUNDED only means: no violation of the checked properties exists within the
approved harness, input constraints and unwind bound (all loops fully unwound, harness
non-vacuous). It is never reported as PROVED.

`leanc generate` exits 0 when generation succeeded (the code is NOT verified yet);
`leanc report` exits with the run's status, or 20 if a successful run's files changed since.
Integrity-gate violations (symbol overrides, tool-divergent preprocessing, termination
calls, ...) are reported as STATIC_ANALYSIS_WARNING by the mandatory `integrity` gate.
"""


# ---------------------------------------------------------------- utilities
class _Out:
    def __init__(self, as_json: bool):
        self.as_json = as_json

    def info(self, msg: str = "") -> None:
        if not self.as_json:
            print(msg)

    def err(self, msg: str) -> None:
        print(msg, file=sys.stderr)

    def emit(self, data: Any) -> None:
        if self.as_json:
            print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _setup_logging(log_file: Path | None, verbose: bool) -> None:
    root = logging.getLogger("leanc")
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.DEBUG if verbose else logging.WARNING)
    sh.setFormatter(logging.Formatter("leanc: %(levelname)s: %(message)s"))
    root.addHandler(sh)


def _explicit_root(args) -> Path | None:
    pr = getattr(args, "project_root", None)
    if not pr:
        return None
    root = Path(pr).resolve()
    if not root.is_dir():
        raise LeanCError(f"--project-root {pr} is not an existing directory")
    return root


def _discover_root(start: Path) -> Path:
    """Nearest ancestor of ``start`` containing leanc.yaml or .leanc/ (else start)."""
    start = start.resolve()
    for d in (start, *start.parents):
        if (d / CONFIG_FILE).is_file() or (d / ".leanc").is_dir():
            return d
    return start


def _spec_and_root(args) -> tuple[Path, Path]:
    spec_path = Path(args.spec)
    if spec_path.is_symlink():
        raise LeanCError(f"{spec_path}: spec must not be a symbolic link")
    if not spec_path.is_file():
        raise LeanCError(f"spec file not found: {spec_path}")
    root = _explicit_root(args) or find_project_root(spec_path)
    return spec_path.resolve(), root


def _verify_options(args, root: Path) -> VerifyOptions:
    cfg = load_config(root)
    gates = None
    if getattr(args, "gates", None):
        gates = [g.strip() for g in args.gates.split(",") if g.strip()]
    sandbox_pref = "none" if getattr(args, "no_sandbox", False) else cfg.sandbox
    if cfg.allow_unsandboxed:
        print(
            "leanc: warning: 'allow_unsandboxed' in leanc.yaml is ignored (a project file cannot disable "
            "isolation); pass --allow-unsandboxed explicitly if you accept the risk",
            file=sys.stderr,
        )
    if getattr(args, "allow_unsandboxed", False):
        print("leanc: WARNING: --allow-unsandboxed: untrusted test programs may run WITHOUT isolation", file=sys.stderr)
    return VerifyOptions(
        allow_unsandboxed=bool(getattr(args, "allow_unsandboxed", False)),
        sandbox_preference=sandbox_pref,
        timeout_scale=float(getattr(args, "timeout_scale", 1.0) or 1.0),
        gates=gates,
        fail_fast=bool(getattr(args, "fail_fast", False)),
        check_unapproved=bool(getattr(args, "check_unapproved", False)),
    )


def _provider(args, root: Path, run_dir: Path):
    cfg = load_config(root)
    name = getattr(args, "provider", None) or cfg.provider
    if getattr(args, "llm_timeout", None):
        os.environ["LEANC_LLM_TIMEOUT"] = str(args.llm_timeout)
    mock_dir = Path(args.mock_dir).resolve() if getattr(args, "mock_dir", None) else root / cfg.mock_dir
    return get_provider(name, project_root=root, mock_dir=mock_dir, transcript_dir=run_dir / "llm")


_ACTIVE_RUN: dict[str, Any] = {}


def _new_run(root: Path, prefix: str) -> tuple[str, Path]:
    run_id = new_run_id(prefix)
    run_dir = ensure_runs_dir(root) / run_id
    run_dir.mkdir(parents=False, exist_ok=False)
    _ACTIVE_RUN.clear()
    _ACTIVE_RUN.update(run_id=run_id, run_dir=run_dir, root=root, command=prefix.rstrip("-"))
    return run_id, run_dir


def _record_aborted_run(message: str) -> None:
    """A run directory created before an input error still gets a run.json /
    report.md explaining why the run stopped (never an empty run folder)."""
    run_dir = _ACTIVE_RUN.get("run_dir")
    if not run_dir or (Path(run_dir) / "run.json").exists():
        return
    try:
        rec = RunRecord(
            run_id=_ACTIVE_RUN["run_id"],
            command=_ACTIVE_RUN["command"],
            leanc_version=__version__,
            status=Status.TOOL_ERROR,
            project_root=str(_ACTIVE_RUN["root"]),
            messages=[f"aborted before completion (input error, exit 2): {message}"],
            finished_at=utcnow(),
        )
        write_json(rec, Path(run_dir) / "run.json")
        (Path(run_dir) / "report.md").write_text(render_run_md(rec), encoding="utf-8")
    except Exception:  # pragma: no cover - best effort
        pass


def _finish_run(record: RunRecord, run_dir: Path, out: _Out, *, exit_override: int | None = None) -> int:
    record.finished_at = utcnow()
    record.artifacts.setdefault("run_json", "run.json")
    record.artifacts.setdefault("report_md", "report.md")
    record.artifacts.setdefault("log", "leanc.log")
    write_json(record, run_dir / "run.json")
    (run_dir / "report.md").write_text(render_run_md(record), encoding="utf-8")
    payload = {
        "run_id": record.run_id,
        "command": record.command,
        "status": record.status.value,
        "exit_code": exit_override if exit_override is not None else exit_code(record.status),
        "provider": record.provider.model_dump() if record.provider else None,
        "run_dir": str(run_dir),
        "report": str(run_dir / "report.md"),
        "messages": record.messages,
    }
    if record.initial_verification is not None:
        payload["initial_status"] = record.initial_verification.status.value
    if record.verification is not None:
        payload["verification"] = summary(record.verification)
    if record.repair_rounds:
        payload["repair_rounds"] = [
            {
                "round": r.round,
                "status_after": r.status_after.value if r.status_after else None,
                "policy_allowed": r.policy.allowed if r.policy else None,
                "violations": [v.rule for v in (r.policy.violations if r.policy else [])],
                "reverted": r.reverted,
                "note": r.note,
            }
            for r in record.repair_rounds
        ]
    out.emit(payload)
    if not out.as_json:
        _print_human_summary(record, run_dir, out)
    for m in record.messages:
        log.info("%s", m)
    log.info("final status %s", record.status.value)
    return exit_override if exit_override is not None else exit_code(record.status)


def _print_human_summary(record: RunRecord, run_dir: Path, out: _Out) -> None:
    vr = record.verification
    if record.provider is not None and not record.provider.real_llm:
        out.info(f"provider  {record.provider.name} (fixed fixtures; NOT a real LLM, not LLM performance)")
    if record.initial_verification is not None:
        out.info(f"initial   {record.initial_verification.status.value}")
    if vr is not None and record.status is Status.REPAIR_EXHAUSTED:
        out.info("best candidate (NOT written to the workspace):")
    if vr is not None:
        out.info(f"spec      {vr.spec_id} v{vr.spec_version}")
        for s in vr.stages:
            out.info(f"  {s.stage:<16} {s.outcome.value:<13} {s.summary[:110]}")
        for f in vr.all_failures()[:8]:
            loc = f.location.short() if f.location else "?"
            out.info(f"  ! [{f.tool}] {f.kind}: {f.description[:90]} @ {loc}")
            if f.inputs:
                out.info("      inputs: " + ", ".join(f"{i.name}={i.value}" for i in f.inputs[:8])[:150])
    for r in record.repair_rounds:
        verdict = "rejected" if r.policy and not r.policy.allowed else (r.status_after.value if r.status_after else "-")
        out.info(f"  repair round {r.round}: {verdict}  {r.note[:100]}")
    for m in record.messages:
        out.info(f"note: {m}")
    out.info(f"STATUS    {record.status.value}")
    out.info(f"report    {run_dir / 'report.md'}")
    out.info(f"log       {run_dir / 'leanc.log'}")


# ---------------------------------------------------------------- commands
def cmd_doctor(args) -> int:
    out = _Out(args.json)
    checks = run_doctor("none" if args.no_sandbox else "auto")
    failed_required = [c for c in checks if c.required and not c.ok]
    out.emit({"ok": not failed_required, "checks": checks_to_json(checks)})
    for c in checks:
        mark = "ok  " if c.ok else ("FAIL" if c.required else "warn")
        out.info(f"[{mark}] {c.name:<22} {c.detail}")
        if not c.ok and c.fix:
            out.info(f"       fix: {c.fix}")
    out.info("")
    out.info("all required checks passed" if not failed_required else f"{len(failed_required)} required check(s) failed")
    return 0 if not failed_required else exit_code(Status.TOOL_ERROR)


def _template_root() -> Path:
    return Path(str(resources.files("leanc") / "data" / "templates"))


def cmd_init(args) -> int:
    out = _Out(args.json)
    dest = Path(args.directory)
    avail = sorted(p.name for p in _template_root().iterdir() if p.is_dir())
    if args.template not in avail:
        raise LeanCError(f"unknown template {args.template!r}; available: {', '.join(avail)}")
    tpl = _template_root() / args.template
    if dest.exists() and not dest.is_dir():
        raise LeanCError(f"{dest} exists and is not a directory")
    if dest.is_symlink():
        raise LeanCError(f"{dest} is a symbolic link")
    dest.mkdir(parents=True, exist_ok=True)
    ws = Workspace(dest)
    plan: list[tuple[str, bytes]] = []
    for src in sorted(tpl.rglob("*")):
        if src.is_dir() or "__pycache__" in src.parts:
            continue
        plan.append((src.relative_to(tpl).as_posix(), src.read_bytes()))
    cfg_text = (
        "# Lean C project configuration (cannot weaken verification; see docs/spec-format.md)\n"
        "provider: mock\nmock_dir: mock\nsandbox: auto\n"
    ).encode()
    plan.append((CONFIG_FILE, cfg_text))
    # Check every target before writing anything (no half-initialised projects).
    conflicts = []
    for rel, _ in plan:
        target = ws.resolve(rel)  # rejects symlinked components
        if target.exists() and not args.force:
            conflicts.append(rel)
    gi_rel = ".gitignore"
    gi_path = ws.resolve(gi_rel)  # pre-check .gitignore too (symlink / dir / encoding)
    if gi_path.exists() and not gi_path.is_file():
        raise LeanCError(f"{gi_path} is not a regular file")
    existing = ws.read_text(gi_rel) if gi_path.exists() else ""
    if conflicts:
        raise LeanCError(f"{len(conflicts)} file(s) already exist in {dest} (use --force): {', '.join(conflicts[:8])}")
    created: list[str] = []
    for rel, data in plan:
        ws.write_bytes(rel, data)
        created.append(rel)
    line = ".leanc/runs/"
    if line not in existing.splitlines():
        sep = "" if not existing or existing.endswith("\n") else "\n"
        ws.write_text(gi_rel, existing + sep + line + "\n")
        created.append(gi_rel)
    dest = ws.root
    out.emit({"directory": str(dest), "template": args.template, "created": created})
    out.info(f"initialised Lean C project in {dest} (template {args.template})")
    for c in created:
        out.info(f"  + {c}")
    out.info("")
    out.info("next steps:")
    out.info("  1. review spec.yaml, the harness and the tests")
    out.info(f"  2. leanc spec approve {dest / 'spec.yaml'} --by <your name>")
    out.info(f"  3. leanc verify {dest / 'spec.yaml'}")
    return 0


def cmd_spec_draft(args) -> int:
    out = _Out(args.json)
    root = _explicit_root(args) or Path(".").resolve()
    if args.requirement_file:
        try:
            requirement = Path(args.requirement_file).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise LeanCError(f"cannot read --requirement-file: {exc}") from None
        except UnicodeDecodeError as exc:
            raise LeanCError(f"--requirement-file is not valid UTF-8 (byte offset {exc.start})") from None
    elif args.requirement:
        requirement = args.requirement
    else:
        raise LeanCError("give --requirement TEXT or --requirement-file FILE")
    run_id, run_dir = _new_run(root, "draft-")
    _setup_logging(run_dir / "leanc.log", args.verbose)
    provider = _provider(args, root, run_dir)
    written = draft_spec(requirement, args.id, root, provider, title=args.title, force=args.force)
    out.emit({"status": Status.SPEC_UNAPPROVED.value, "trusted": False, "written": written, "provider": provider.info.model_dump()})
    out.info("wrote an UNTRUSTED spec draft (it has no effect until approved):")
    for k, v in written.items():
        if k != "validation":
            out.info(f"  {k:<8} {root / v}")
    out.info(f"schema validation: {written.get('validation')}")
    if not provider.info.real_llm:
        out.info("note: produced by the mock provider (fixed fixtures), not by a real LLM")
    return exit_code(Status.SPEC_UNAPPROVED)


def cmd_spec_approve(args) -> int:
    out = _Out(args.json)
    spec_path, root = _spec_and_root(args)
    spec = load_spec(spec_path)
    ws = Workspace(root)
    rel = ws.relpath(spec_path)
    files = protected_file_hashes(spec, ws)
    err = sys.stderr  # interactive review goes to stderr so --json stdout stays pure JSON
    if spec.provenance.origin == "ai-draft":
        print(
            "leanc: WARNING: this spec is an AI DRAFT (provenance.origin: ai-draft). Approving makes it, its harness "
            "and its tests the trusted verification standard.",
            file=err,
        )
    if not args.yes:
        if not sys.stdin.isatty():
            raise LeanCError("refusing to approve non-interactively; review the spec and pass --yes")
        print(f"Approving spec {spec.spec_id} v{spec.version} (sha256 {spec_hash(spec)})", file=err)
        prev = approved_snapshot(root, spec.spec_id)
        if prev:
            changes = diff_spec_dicts(prev, spec_canonical_dict(spec))
            print("changes since the last approved version:" if changes else "no field changes since last approval", file=err)
            for c in changes[:50]:
                print("  " + c, file=err)
        print("protected files (these become trusted and hash-locked):", file=err)
        for p, h in files.items():
            print(f"  {p}  {h[:16]}", file=err)
        print("type 'approve' to confirm: ", end="", file=err, flush=True)
        ans = input().strip()
        if ans != "approve":
            out.err("not approved")
            return exit_code(Status.SPEC_UNAPPROVED)
    rec = approve_spec(spec, rel, ws, approved_by=args.by, note=args.note or "")
    out.emit({k: v for k, v in rec.items() if k != "spec_snapshot"})
    out.info(f"approved {spec.spec_id} v{spec.version} by {rec['approved_by']} (sha256 {rec['spec_hash'][:16]}...)")
    for p, h in files.items():
        out.info(f"  locked {p}  {h[:16]}")
    return 0


def cmd_spec_status(args) -> int:
    out = _Out(args.json)
    spec_path, root = _spec_and_root(args)
    spec = load_spec(spec_path)
    chk = check_approval(spec, Workspace(root))
    prev = approved_snapshot(root, spec.spec_id)
    changes = diff_spec_dicts(prev, spec_canonical_dict(spec)) if prev else []
    out.emit({**chk.model_dump(mode="json"), "changes_since_approval": changes, "origin": spec.provenance.origin})
    out.info(f"spec {spec.spec_id} v{spec.version}  origin={spec.provenance.origin}")
    out.info(f"status: {'APPROVED' if chk.approved else 'SPEC_UNAPPROVED'}")
    if chk.approved:
        out.info(f"approved by {chk.approved_by} at {chk.approved_at}")
    for r in chk.reasons:
        out.info(f"  - {r}")
    for c in changes[:50]:
        out.info(f"  {c}")
    return 0 if chk.approved else exit_code(Status.SPEC_UNAPPROVED)


def cmd_spec_show(args) -> int:
    out = _Out(args.json)
    spec = load_spec(args.spec)
    out.emit({"spec_hash": spec_hash(spec), "spec": spec_canonical_dict(spec), "protected_files": spec.protected_paths()})
    out.info(f"# sha256 {spec_hash(spec)}")
    out.info(f"# protected files: {', '.join(spec.protected_paths())}")
    if spec.verification is not None:
        out.info(f"# effective CBMC checks: {', '.join(spec.verification.effective_checks())}")
    out.info(f"# gates: {', '.join(spec.gates)} (+ mandatory integrity gate after compile)")
    out.info(spec_to_yaml(spec))
    return 0


def cmd_generate(args) -> int:
    out = _Out(args.json)
    spec_path, root = _spec_and_root(args)
    spec = load_spec(spec_path)
    run_id, run_dir = _new_run(root, "gen-")
    _setup_logging(run_dir / "leanc.log", args.verbose)
    provider = _provider(args, root, run_dir)  # ProviderError here: readable error, exit 2
    rec = RunRecord(
        run_id=run_id,
        command="generate",
        leanc_version=__version__,
        status=Status.UNKNOWN,
        spec_path=str(spec_path),
        spec_id=spec.spec_id,
        project_root=str(root),
        provider=provider.info,
    )
    try:
        gen = generate_sources(spec, root, provider, run_dir=run_dir, force=args.force)
    except GenerationRejected as exc:
        rec.status = Status.SPEC_UNAPPROVED if str(exc).startswith("SPEC_UNAPPROVED") else Status.TOOL_ERROR
        rec.messages.append(str(exc))
        return _finish_run(rec, run_dir, out)
    except ProviderError as exc:
        rec.status = Status.TOOL_ERROR
        rec.messages.append(f"provider error: {exc}")
        return _finish_run(rec, run_dir, out)
    rec.generation = gen
    rec.messages.append(
        f"generated {len(gen.files)} file(s): {', '.join(gen.files) or '(none)'}; "
        "generated code is unverified until `leanc verify` passes"
    )
    if gen.note:
        rec.messages.append(gen.note)
    # Generation itself verifies nothing: the record says UNKNOWN (nothing was
    # verified), but the command succeeded, so it exits 0 (see --help).
    rec.status = Status.UNKNOWN
    return _finish_run(rec, run_dir, out, exit_override=0)


def _do_verify(args, spec_path: Path, root: Path, run_id: str, run_dir: Path):
    return verify(
        spec_path,
        project_root=root,
        options=_verify_options(args, root),
        run_dir=run_dir / "verify",
        run_id=run_id,
    )


def cmd_verify(args) -> int:
    out = _Out(args.json)
    spec_path, root = _spec_and_root(args)
    spec = load_spec(spec_path)
    run_id, run_dir = _new_run(root, "verify-")
    _setup_logging(run_dir / "leanc.log", args.verbose)
    vr = _do_verify(args, spec_path, root, run_id, run_dir)
    rec = RunRecord(
        run_id=run_id,
        command="verify",
        leanc_version=__version__,
        status=vr.status,
        spec_path=str(spec_path),
        spec_id=spec.spec_id,
        project_root=str(root),
        verification=vr,
        initial_verification=None,
    )
    return _finish_run(rec, run_dir, out)


def _repair_and_record(args, rec: RunRecord, spec_path: Path, spec, root: Path, run_dir: Path, initial) -> None:
    if is_success(initial.status) or (initial.status not in REPAIRABLE and not proof_repairable(initial)):
        provider = None  # no LLM needed (and none is constructed)
    else:
        try:
            provider = _provider(args, root, run_dir)
        except ProviderError as exc:
            rec.status = Status.TOOL_ERROR
            rec.messages.append(f"repair not attempted: LLM provider unavailable: {exc} "
                                f"(verification status: {initial.status.value})")
            return
        rec.provider = provider.info
    outcome = repair_loop(
        spec_path,
        spec,
        root,
        provider,
        initial,
        run_dir=run_dir,
        options=_verify_options(args, root),
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
            "--no-write: the workspace was NOT changed and is NOT verified; the verified candidate is in "
            f"{run_dir / 'verified-candidate'}"
        )


def cmd_repair(args) -> int:
    out = _Out(args.json)
    spec_path, root = _spec_and_root(args)
    spec = load_spec(spec_path)
    run_id, run_dir = _new_run(root, "repair-")
    _setup_logging(run_dir / "leanc.log", args.verbose)
    initial = _do_verify(args, spec_path, root, run_id, run_dir)
    rec = RunRecord(
        run_id=run_id,
        command="repair",
        leanc_version=__version__,
        status=initial.status,
        spec_path=str(spec_path),
        spec_id=spec.spec_id,
        project_root=str(root),
        initial_verification=initial,
        verification=initial,
    )
    _repair_and_record(args, rec, spec_path, spec, root, run_dir, initial)
    return _finish_run(rec, run_dir, out)


def cmd_run(args) -> int:
    out = _Out(args.json)
    spec_path, root = _spec_and_root(args)
    spec = load_spec(spec_path)
    run_id, run_dir = _new_run(root, "run-")
    _setup_logging(run_dir / "leanc.log", args.verbose)
    rec = RunRecord(
        run_id=run_id,
        command="run",
        leanc_version=__version__,
        status=Status.UNKNOWN,
        spec_path=str(spec_path),
        spec_id=spec.spec_id,
        project_root=str(root),
    )
    # 1. specification check (no generation for an unapproved spec)
    chk = check_approval(spec, Workspace(root))
    if not chk.approved:
        rec.status = Status.SPEC_UNAPPROVED
        rec.messages.append("spec is not approved: " + "; ".join(chk.reasons))
        rec.messages.append(f"review the spec, harness and tests, then: leanc spec approve {spec_path} --by <name>")
        return _finish_run(rec, run_dir, out)
    # 2. generation (only missing sources, or all with --regenerate)
    ws = Workspace(root)
    if args.regenerate or any(not ws.exists(p) for p in spec.sources.editable):
        try:
            provider = _provider(args, root, run_dir)
            rec.provider = provider.info
            rec.generation = generate_sources(spec, root, provider, run_dir=run_dir, force=args.regenerate)
            rec.messages.append(f"generated: {', '.join(rec.generation.files) or '(nothing)'}")
            if rec.generation.note:
                rec.messages.append(rec.generation.note)
        except GenerationRejected as exc:
            rec.status = Status.TOOL_ERROR
            rec.messages.append(str(exc))
            return _finish_run(rec, run_dir, out)
        except ProviderError as exc:
            rec.status = Status.TOOL_ERROR
            rec.messages.append(f"provider error during generation: {exc}")
            return _finish_run(rec, run_dir, out)
    # 3-5. compile -> integrity -> analyse -> sanitize -> CBMC
    initial = _do_verify(args, spec_path, root, run_id, run_dir)
    rec.initial_verification = initial
    rec.verification = initial
    rec.status = initial.status
    # 6. repair -> re-verify
    if not is_success(initial.status) and not args.no_repair:
        _repair_and_record(args, rec, spec_path, spec, root, run_dir, initial)
    return _finish_run(rec, run_dir, out)


def _find_run(arg: str | None, root: Path) -> Path:
    if arg and Path(arg).exists():
        p = Path(arg)
        return p.parent if p.is_file() else p
    rd = runs_dir(root)
    if not rd.is_dir():
        raise LeanCError(f"no runs found under {rd}")
    if not arg or arg == "latest":
        cands = [d for d in rd.iterdir() if d.is_dir() and (d / "run.json").exists()]
        if not cands:
            raise LeanCError(f"no runs found under {rd}")
        return max(cands, key=lambda d: (d / "run.json").stat().st_mtime)
    p = rd / arg
    if not p.is_dir():
        raise LeanCError(f"run {arg!r} not found under {rd}")
    return p


def _stale_files(rr: RunRecord) -> list[str]:
    """Files whose current content differs from what the run verified."""
    vr = rr.verification
    if vr is None or not vr.source_hashes or not rr.project_root:
        return []
    try:
        ws = Workspace(rr.project_root)
    except LeanCError:
        return sorted(vr.source_hashes)
    stale = []
    if rr.spec_path and vr.spec_hash:
        try:
            if spec_hash(load_spec(rr.spec_path)) != vr.spec_hash:
                stale.append(f"{rr.spec_path} (spec content)")
        except LeanCError:
            stale.append(f"{rr.spec_path} (spec unreadable)")
    for rel, h in vr.source_hashes.items():
        try:
            cur = sha256_bytes(ws.read_bytes(rel))
        except LeanCError:
            stale.append(rel)
            continue
        if cur != h:
            stale.append(rel)
    return stale


def cmd_report(args) -> int:
    root = _explicit_root(args) or _discover_root(Path("."))
    run = _find_run(args.run, root)
    rr = read_run_record(run / "run.json")
    stale = _stale_files(rr)
    if args.format == "json":
        data = rr.model_dump(mode="json")
        if rr.verification:
            data["summary"] = summary(rr.verification)
        data["stale_files"] = stale
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(render_run_md(rr))
    if stale:
        print(
            "leanc: warning: STALE: the current workspace differs from what this run verified, so its status does "
            "not describe the current workspace: " + ", ".join(stale),
            file=sys.stderr,
        )
        if is_success(rr.status):
            return exit_code(Status.UNKNOWN)  # never vouch for code that was not verified
    return 0 if rr.command == "generate" and rr.status is Status.UNKNOWN else exit_code(rr.status)


# ---------------------------------------------------------------- parser
def _add_common(p: argparse.ArgumentParser, *, spec: bool = True, verify: bool = False, provider: bool = False) -> None:
    if spec:
        p.add_argument("spec", help="path to the spec (YAML or JSON)")
        p.add_argument("--project-root", help="project root (default: nearest dir with leanc.yaml, else the spec's dir)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON on stdout")
    p.add_argument("-v", "--verbose", action="store_true", help="verbose logging to stderr")
    if verify:
        g = p.add_argument_group("verification")
        g.add_argument("--gates", help="comma-separated subset of the spec's gates (result cannot be VERIFIED_BOUNDED)")
        g.add_argument("--fail-fast", action="store_true", help="stop after the first gate that does not pass")
        g.add_argument("--timeout-scale", type=float, default=1.0, help="multiply every tool timeout (default 1.0)")
        g.add_argument("--allow-unsandboxed", action="store_true",
                       help="DANGEROUS: execute untrusted test programs even when no sandbox is available")
        g.add_argument("--no-sandbox", action="store_true",
                       help="do not use bwrap (dynamic tests are then skipped unless --allow-unsandboxed)")
        g.add_argument("--check-unapproved", action="store_true",
                       help="run gates for information even if the spec is unapproved (status stays SPEC_UNAPPROVED)")
    if provider:
        g = p.add_argument_group("LLM provider")
        g.add_argument("--provider", help="mock | openai (default from leanc.yaml, else mock)")
        g.add_argument("--mock-dir", help="fixture directory for the mock provider (default <root>/mock)")
        g.add_argument("--llm-timeout", type=float, help="LLM request timeout in seconds")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="leanc",
        description="Lean C: spec-first, verifier-checked AI code generation for C17 (research prototype)",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"leanc {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    d = sub.add_parser("doctor", help="check Python, Clang, analyzer, sanitizers, CBMC, sandbox, LLM config")
    _add_common(d, spec=False)
    d.add_argument("--no-sandbox", action="store_true", help="skip the sandbox check")
    d.set_defaults(func=cmd_doctor)

    i = sub.add_parser("init", help="create a project from a template")
    i.add_argument("directory", nargs="?", default=".")
    i.add_argument("--template", default="safe-array-get", help="template name (default safe-array-get)")
    i.add_argument("--force", action="store_true", help="overwrite existing files")
    _add_common(i, spec=False)
    i.set_defaults(func=cmd_init)

    s = sub.add_parser("spec", help="draft / approve / inspect specifications")
    ssub = s.add_subparsers(dest="spec_command", metavar="<action>")
    sd = ssub.add_parser("draft", help="turn a natural-language requirement into an UNTRUSTED spec draft")
    sd.add_argument("--id", required=True, help="spec id for the draft")
    sd.add_argument("--requirement", help="requirement text")
    sd.add_argument("--requirement-file", help="file containing the requirement")
    sd.add_argument("--title")
    sd.add_argument("--project-root", help="project root (default: current directory)")
    sd.add_argument("--force", action="store_true")
    _add_common(sd, spec=False, provider=True)
    sd.set_defaults(func=cmd_spec_draft)
    sa = ssub.add_parser("approve", help="approve (hash-lock and sign) a reviewed spec, harness and tests")
    sa.add_argument("--by", required=True, help="name of the human approver")
    sa.add_argument("--note", help="approval note")
    sa.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    _add_common(sa)
    sa.set_defaults(func=cmd_spec_approve)
    st = ssub.add_parser("status", help="show approval status and changes since approval")
    _add_common(st)
    st.set_defaults(func=cmd_spec_status)
    sh = ssub.add_parser("show", help="print the normalised spec, its hash and effective checks")
    _add_common(sh)
    sh.set_defaults(func=cmd_spec_show)

    g = sub.add_parser("generate", help="generate the editable C sources for an approved spec")
    g.add_argument("--force", action="store_true", help="regenerate existing editable sources")
    _add_common(g, provider=True)
    g.set_defaults(func=cmd_generate)

    v = sub.add_parser("verify", help="compile, analyse, sanitize and model-check the current sources")
    _add_common(v, verify=True)
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("repair", help="verify, then run the counterexample-driven repair loop")
    r.add_argument("--max-rounds", type=int, help="override spec.repair.max_rounds (cannot exceed 20)")
    r.add_argument("--no-write", action="store_true", help="do not write the verified candidate back to the workspace")
    _add_common(r, verify=True, provider=True)
    r.set_defaults(func=cmd_repair)

    rn = sub.add_parser("run", help="full workflow: spec check -> generate -> verify -> repair -> re-verify -> report")
    rn.add_argument("--max-rounds", type=int, help="override spec.repair.max_rounds (cannot exceed 20)")
    rn.add_argument("--regenerate", action="store_true", help="regenerate editable sources even if they exist")
    rn.add_argument("--no-repair", action="store_true", help="stop after the first verification")
    rn.add_argument("--no-write", action="store_true", help="do not write the verified candidate back to the workspace")
    _add_common(rn, verify=True, provider=True)
    rn.set_defaults(func=cmd_run)

    rp = sub.add_parser("report", help="show the report of a run (default: latest)")
    rp.add_argument("run", nargs="?", help="run id, run directory, run.json path, or 'latest'")
    rp.add_argument("--project-root", help="project root (default: current directory)")
    rp.add_argument("--format", choices=["md", "json"], default="md")
    rp.set_defaults(func=cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        if args.command == "spec":
            parser._subparsers._group_actions[0].choices["spec"].print_help()  # type: ignore[union-attr]
        else:
            parser.print_help()
        return EXIT_USAGE
    if getattr(args, "max_rounds", None) is not None and not (0 <= args.max_rounds <= 20):
        print("leanc: error: --max-rounds must be between 0 and 20", file=sys.stderr)
        return EXIT_USAGE
    if getattr(args, "timeout_scale", None) is not None and not (0.01 <= args.timeout_scale <= 100):
        print("leanc: error: --timeout-scale must be between 0.01 and 100", file=sys.stderr)
        return EXIT_USAGE
    if not logging.getLogger("leanc").handlers:
        _setup_logging(None, getattr(args, "verbose", False))
    try:
        return int(args.func(args))
    except LeanCError as exc:
        log.info("error: %s", exc)  # log file only; printed once below
        _record_aborted_run(str(exc))
        print(f"leanc: error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("leanc: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # pragma: no cover - last resort
        log.exception("internal error")
        print(f"leanc: internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
