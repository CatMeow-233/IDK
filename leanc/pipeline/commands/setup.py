from __future__ import annotations

from importlib import resources
from pathlib import Path

from leanc.foundation.errors import LeanCError
from leanc.foundation.status import Status, exit_code
from leanc.foundation.workspace import Workspace
from leanc.pipeline.commands.console import Console
from leanc.pipeline.config import CONFIG_FILE
from leanc.pipeline.doctor import Check, checks_to_json, run_doctor

GITIGNORE = ".gitignore"
GITIGNORE_RUNS_LINE = ".leanc/runs/"
DEFAULT_PROJECT_CONFIG = (
    "# Lean C project configuration (cannot weaken verification; see docs/spec-format.md)\n"
    "provider: mock\nmock_dir: mock\nsandbox: auto\n"
).encode()
SHOWN_CONFLICTS = 8


def _doctor_mark(check: Check) -> str:
    if check.ok:
        return "ok  "
    return "FAIL" if check.required else "warn"


def cmd_doctor(args) -> int:
    console = Console(args.json)
    checks = run_doctor("none" if args.no_sandbox else "auto")
    failed_required = [c for c in checks if c.required and not c.ok]
    console.emit({"ok": not failed_required, "checks": checks_to_json(checks)})
    for check in checks:
        console.info(f"[{_doctor_mark(check)}] {check.name:<22} {check.detail}")
        if not check.ok and check.fix:
            console.info(f"       fix: {check.fix}")
    console.info("")
    console.info(
        "all required checks passed" if not failed_required else f"{len(failed_required)} required check(s) failed"
    )
    return 0 if not failed_required else exit_code(Status.TOOL_ERROR)


def _template_root() -> Path:
    return Path(str(resources.files("leanc") / "data" / "templates"))


def _template_files(template: Path) -> list[tuple[str, bytes]]:
    return [
        (src.relative_to(template).as_posix(), src.read_bytes())
        for src in sorted(template.rglob("*"))
        if not src.is_dir() and "__pycache__" not in src.parts
    ]


def _existing_files_without_force(ws: Workspace, plan: list[tuple[str, bytes]], force: bool) -> list[str]:
    return [rel for rel, _ in plan if ws.resolve(rel).exists() and not force]


def _existing_gitignore(ws: Workspace) -> str:
    path = ws.resolve(GITIGNORE)
    if path.exists() and not path.is_file():
        raise LeanCError(f"{path} is not a regular file")
    return ws.read_text(GITIGNORE) if path.exists() else ""


def _prepared_destination(directory: str) -> Path:
    dest = Path(directory)
    if dest.exists() and not dest.is_dir():
        raise LeanCError(f"{dest} exists and is not a directory")
    if dest.is_symlink():
        raise LeanCError(f"{dest} is a symbolic link")
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def _write_project(ws: Workspace, plan: list[tuple[str, bytes]], gitignore: str) -> list[str]:
    created: list[str] = []
    for rel, data in plan:
        ws.write_bytes(rel, data)
        created.append(rel)
    if GITIGNORE_RUNS_LINE not in gitignore.splitlines():
        separator = "" if not gitignore or gitignore.endswith("\n") else "\n"
        ws.write_text(GITIGNORE, gitignore + separator + GITIGNORE_RUNS_LINE + "\n")
        created.append(GITIGNORE)
    return created


def cmd_init(args) -> int:
    console = Console(args.json)
    available = sorted(p.name for p in _template_root().iterdir() if p.is_dir())
    if args.template not in available:
        raise LeanCError(f"unknown template {args.template!r}; available: {', '.join(available)}")
    template = _template_root() / args.template
    dest = _prepared_destination(args.directory)
    ws = Workspace(dest)
    plan = _template_files(template) + [(CONFIG_FILE, DEFAULT_PROJECT_CONFIG)]
    conflicts = _existing_files_without_force(ws, plan, args.force)
    gitignore = _existing_gitignore(ws)
    if conflicts:
        raise LeanCError(
            f"{len(conflicts)} file(s) already exist in {dest} (use --force): {', '.join(conflicts[:SHOWN_CONFLICTS])}"
        )
    created = _write_project(ws, plan, gitignore)
    dest = ws.root
    console.emit({"directory": str(dest), "template": args.template, "created": created})
    console.info(f"initialised Lean C project in {dest} (template {args.template})")
    for rel in created:
        console.info(f"  + {rel}")
    console.info("")
    console.info("next steps:")
    console.info("  1. review spec.yaml, the harness and the tests")
    console.info(f"  2. leanc spec approve {dest / 'spec.yaml'} --by <your name>")
    console.info(f"  3. leanc verify {dest / 'spec.yaml'}")
    return 0
