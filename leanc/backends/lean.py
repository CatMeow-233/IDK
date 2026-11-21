"""Lean 4 model-proof gate (V2, optional).

What this gate establishes
--------------------------
``spec.lean.spec_file`` (protected, approved) defines a *mathematical model*
of the target and theorem **statements** as ``def <name> : Prop := ...``.
``spec.lean.proofs`` (editable: the AI may write and repair them) must define
the proof constants named in ``spec.lean.theorems``.  The gate PASSes only if

* every proof constant exists in a proof file, is a theorem (or a safe
  definition) without universe parameters, and the **Lean kernel** accepts it
  as a proof of exactly the approved statement (a generated trusted checker
  adds ``theorem _ : <statement> := <proof>`` with ``addDecl``: no name
  resolution, no elaborator, defeq decided by the kernel);
* the proof depends on the standard axioms only (``propext``,
  ``Classical.choice``, ``Quot.sound``): ``sorryAx``, ``Lean.ofReduceBool``
  (``native_decide``), ``Lean.trustCompiler`` or any user axiom leave the
  theorem *unproved*.  Axioms are collected twice - by Lean's
  ``collectAxioms`` and by the checker's own walk over the constant bodies -
  because ``collectAxioms`` trusts per-module tables stored in ``.olean``
  files;
* the proof modules survive an independent kernel replay
  (``leanchecker``, shipped with the toolchain), which detects declarations
  that entered the environment without kernel checking (the checker trusts
  the ``.olean`` files it imports, so without ``leanchecker`` the gate never
  PASSes);
* when ``spec.lean.vectors`` is configured, the C implementation agrees with
  the model on every vector printed by the trusted generator (run with
  ``lean --run``), as judged by the trusted C driver built with the
  sanitizer flags of the spec.

A Lean result is about the MODEL.  It never makes the C verdict ``PROVED``
(the controller only derives PROVED from the ``wp`` gate); the model is
linked to the C code only by the differential test vectors, which is stated
in ``details["assumptions"]``.

Outcomes: vector mismatch / sanitizer report -> FAIL (RUNTIME_TEST_FAILED);
a theorem unproved / missing / using a forbidden axiom, or a proof file
rejected by the Lean policy -> INCONCLUSIVE (UNKNOWN; proof-repairable);
missing Lean, an approved spec file that does not compile, malformed tool
output -> ERROR; budget exhausted -> TIMEOUT.

Isolation
---------
Elaborating Lean runs code at compile time (macros, ``#eval``,
``initialize``, elaborators can do IO).  Therefore every Lean invocation -
including the trusted spec file - runs through :meth:`BackendContext.run`
in the sandbox (no network) with the toolchain bound read-only, and only
when :meth:`BackendContext.may_execute_untrusted` allows untrusted code.
Each compilation step writes into its own fresh output directory; earlier
outputs are mounted read-only, and after each step the directory must
contain exactly the expected ``.olean`` (anything else -> ERROR), so a proof
file cannot plant or replace modules for later steps.  Before anything
runs, the proof files must pass the Lean rules of
:mod:`leanc.rules.lean_rules` (no ``axiom``/``unsafe``/``set_option
debug.*``/``#eval``/``macro``/``instance``/foreign imports, ...).

Module layout: see :func:`leanc.rules.lean_rules.lean_layout` (all Lean
files under one root directory; ``lean/Spec.lean`` is module ``Spec``).
Proof files are compiled in the order of ``spec.lean.proofs``; a proof file
may import the spec module and *earlier* proof files.

Vector protocol (generator / driver are trusted, protected files)
-----------------------------------------------------------------
* The generator is a Lean program (``def main : IO Unit``) importing the
  spec module; it prints one vector per line (blank lines and lines starting
  with ``#`` are ignored).  At most :data:`MAX_VECTOR_BYTES` bytes.
* The driver (C, compiled with the editable sources) reads the vectors on
  stdin.  For each disagreement it prints a line
  ``LEANC-VECTOR-MISMATCH key=value ...`` (stdout or stderr) and finally
  exits non-zero; on success it prints exactly one line
  ``LEANC-VECTORS-CHECKED <n>`` with ``n`` = number of vectors checked and
  exits 0.  ``n`` must equal the number of vectors generated.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import shutil
import signal as _signal
import time
from dataclasses import dataclass, field
from pathlib import Path

import leanc.backends.sanitizer as san
from leanc.foundation.models import (
    CounterexampleInput,
    Diagnostic,
    FailureReport,
    Location,
    StageResult,
    ToolInvocation,
)
from leanc.foundation.redact import redact
from leanc.foundation.runner import DEFAULT_PATH, Limits, ProcResult
from leanc.foundation.sandbox import SandboxMounts
from leanc.foundation.status import Outcome, Status, outcome_to_status
from leanc.rules.lean_rules import LeanLayout, lean_layout, lean_source_violations
from leanc.rules.source import source_violations
from leanc.toolchain.context import BackendContext
from leanc.toolchain.cxx import compiler, compiler_version
from leanc.toolchain.lean import INSTALL_HINT, LeanToolchain, find_lean

STAGE = "lean"
#: The only axioms a proof may depend on (Lean's standard foundations).
STANDARD_AXIOMS: tuple[str, ...] = ("propext", "Classical.choice", "Quot.sound")
#: RLIMIT_AS for Lean processes: Lean maps .olean files (``import Lean`` maps
#: well over 4 GiB of address space) and reserves thread stacks, so the limit
#: bounds address space generously; real memory is bounded by ``--memory``.
LEAN_ADDRESS_SPACE_MB = 16384
#: ``lean --memory`` (Lean's own allocation limit, MB).
LEAN_HEAP_MB = 8192
MAX_LEAN_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_VECTOR_BYTES = 8 * 1024 * 1024
MAX_REPORTED_MISMATCHES = 20
VECTOR_MISMATCH_TAG = "LEANC-VECTOR-MISMATCH"
VECTOR_CHECKED_TAG = "LEANC-VECTORS-CHECKED"
CHECKER_FILE = "LeancCheck.lean"

_MISMATCH_RE = re.compile(rf"^\s*{VECTOR_MISMATCH_TAG}\b(.*)$", re.MULTILINE)
_CHECKED_RE = re.compile(rf"^\s*{VECTOR_CHECKED_TAG}\s+(\S+)\s*$", re.MULTILINE)
_KV_RE = re.compile(r"([A-Za-z_][\w.\-]*)=(\S*)")


# =========================================================================== Lean messages
def parse_lean_messages(stdout: str) -> tuple[list[dict], list[str]]:
    """``lean --json`` prints one JSON message per line.  Returns (messages,
    non-JSON lines); the latter are kept for the raw log only."""
    msgs: list[dict] = []
    other: list[str] = []
    for line in stdout.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("{"):
            try:
                obj = json.loads(s)
            except ValueError:
                other.append(line)
                continue
            if isinstance(obj, dict) and "severity" in obj and "data" in obj:
                msgs.append(obj)
                continue
        other.append(line)
    return msgs, other


def _msg_location(msg: dict) -> Location | None:
    pos = msg.get("pos") if isinstance(msg.get("pos"), dict) else None
    file = msg.get("fileName")
    if not file and not pos:
        return None
    return Location(
        file=str(file) if file else None,
        line=int(pos["line"]) if pos and isinstance(pos.get("line"), int) else None,
        column=int(pos["column"]) + 1 if pos and isinstance(pos.get("column"), int) else None,
    )


def _msg_diagnostic(msg: dict) -> Diagnostic:
    sev = {"error": "error", "warning": "warning"}.get(str(msg.get("severity")), "info")
    kind = str(msg.get("kind") or "")
    rule = kind if kind and kind != "[anonymous]" else f"lean-{msg.get('severity', 'message')}"
    text = str(msg.get("data", "")).strip()
    return Diagnostic(
        tool="lean",
        rule_id=rule,
        severity=sev,  # type: ignore[arg-type]
        message=text[:2000],
        location=_msg_location(msg),
    )


# =========================================================================== checker
def _lean_name(name: str) -> str:
    """A Lean ``Name`` term built from string components (robust against
    components that are keywords; components are validated identifiers)."""
    parts = ", ".join(json.dumps(p) for p in name.split("."))
    return f"(LeancCheck.nm [{parts}])"


def _import_line(module: str) -> str:
    return "import " + ".".join(f"«{p}»" for p in module.split("."))


def render_checker(nonce: str, spec_module: str, proof_modules: list[str], theorems: list[tuple[str, str]]) -> str:
    """The trusted checker (generated per run; ``nonce`` tags its results)."""
    imports = "\n".join(_import_line(m) for m in [spec_module, *proof_modules])
    pmods = ", ".join(_lean_name(m) for m in proof_modules)
    calls = "\n".join(
        f"run_cmd LeancCheck.check {i} {_lean_name(stmt)} {_lean_name(proof)} {_lean_name(spec_module)} #[{pmods}]"
        for i, (stmt, proof) in enumerate(theorems)
    )
    return _CHECKER_TEMPLATE.format(imports=imports, nonce=nonce, calls=calls)


_CHECKER_TEMPLATE = """\
import Lean
{imports}

-- Generated by Lean C (trusted).  Results are logged as JSON tagged with a per-run nonce.
set_option Elab.async false

namespace LeancCheck
open Lean Elab Command

def nm (xs : List String) : Name := xs.foldl Name.mkStr Name.anonymous

def nameMem (n : Name) (xs : Array Name) : Bool := xs.any (fun m => Name.beq n m)

def deps (ci : ConstantInfo) : Array Name :=
  let a := ci.type.getUsedConstants
  let b := match ci.value? (allowOpaque := true) with
    | some v => v.getUsedConstants
    | none => #[]
  let c : Array Name := match ci with
    | .inductInfo v => v.ctors.toArray
    | .ctorInfo v => #[v.induct]
    | .recInfo v => (v.rules.toArray.map (fun r => r.rhs.getUsedConstants)).flatten
    | _ => #[]
  a ++ b ++ c

/-- Axioms reachable from `root`, found by walking the constant bodies themselves
(independent of the precomputed per-module axiom tables stored in .olean files). -/
def walk (env : Environment) (root : Name) : Array Name × Array Name × Nat := Id.run do
  let env := env.setExporting false
  let mut todo : Array Name := #[root]
  let mut seen : NameSet := NameSet.empty
  let mut axs : Array Name := #[]
  let mut miss : Array Name := #[]
  let mut n := 0
  while !todo.isEmpty do
    let c := todo.back!
    todo := todo.pop
    if seen.contains c then continue
    seen := seen.insert c
    n := n + 1
    match env.find? c with
    | none => miss := miss.push c
    | some ci =>
      if ci matches .axiomInfo _ then axs := axs.push c
      for d in deps ci do
        unless seen.contains d do todo := todo.push d
  return (axs, miss, n)

def emit (idx : Nat) (status detail : String) (axs miss : Array Name) (n : Nat) : CommandElabM Unit := do
  let j := Json.mkObj [("leanc", Json.str "{nonce}"), ("idx", Json.num (JsonNumber.fromNat idx)),
    ("status", Json.str status), ("detail", Json.str detail),
    ("axioms", Json.arr (axs.map (fun a => Json.str (Name.toString a)))),
    ("missing", Json.arr (miss.map (fun a => Json.str (Name.toString a)))),
    ("walked", Json.num (JsonNumber.fromNat n))]
  logInfo (MessageData.ofFormat (Std.Format.text (Json.compress j)))

def union (a b : Array Name) : Array Name := b.foldl (fun acc x => if nameMem x acc then acc else acc.push x) a

def check (idx : Nat) (stmt proof specMod : Name) (proofMods : Array Name) : CommandElabM Unit := do
  let env ← getEnv
  let modOf := fun (c : Name) => (env.getModuleIdxFor? c).map (fun i => env.header.moduleNames[i.toNat]!)
  let some sci := env.find? stmt | emit idx "spec-error" "the statement is not defined" #[] #[] 0
  unless (modOf stmt).any (fun m => Name.beq m specMod) do
    emit idx "spec-error" "the statement is not defined in the Lean spec file" #[] #[] 0; return
  unless sci matches .defnInfo _ do
    emit idx "spec-error" "the statement is not a `def`" #[] #[] 0; return
  unless sci.type.isProp && sci.levelParams.isEmpty do
    emit idx "spec-error" "the statement is not a `Prop` without universe parameters" #[] #[] 0; return
  let (saxs, smiss, _) := walk env stmt
  let saxs := union saxs (← liftCoreM <| Lean.collectAxioms stmt)
  unless smiss.isEmpty && saxs.all (fun a => nameMem a #[`propext, `Classical.choice, `Quot.sound]) do
    emit idx "spec-error" "the statement itself depends on non-standard axioms" saxs smiss 0; return
  let some pci := env.find? proof | emit idx "unproved" "the proof constant is not defined" #[] #[] 0
  unless (modOf proof).any (fun m => nameMem m proofMods) do
    emit idx "unproved" "the proof constant is not defined in a (successfully compiled) proof file" #[] #[] 0; return
  let okKind := match pci with
    | .thmInfo _ => true
    | .defnInfo v => match v.safety with
      | .safe => true
      | _ => false
    | _ => false
  unless okKind do
    emit idx "unproved" "the proof constant is not a theorem or a safe definition (axiom/opaque/unsafe)" #[] #[] 0; return
  unless pci.levelParams.isEmpty do
    emit idx "unproved" "the proof constant has universe parameters" #[] #[] 0; return
  let chk := Name.mkStr `LeancCheck ("check_" ++ Nat.repr idx)
  try
    liftCoreM <| addDecl (Declaration.thmDecl
      {{ name := chk, levelParams := [], type := mkConst stmt, value := mkConst proof }})
  catch e =>
    emit idx "unproved" ("the kernel rejected the proof for the approved statement: " ++ (← e.toMessageData.toString)) #[] #[] 0
    return
  let (axs, miss, n) := walk (← getEnv) proof
  let axs := union axs (← liftCoreM <| Lean.collectAxioms proof)
  emit idx "checked" "" axs miss n

end LeancCheck

{calls}
"""


# =========================================================================== results
@dataclass
class _TheoremResult:
    name: str
    statement: str
    proof: str
    proved: bool = False
    axioms: list[str] = field(default_factory=list)
    reason: str = ""
    spec_error: str | None = None

    def as_dict(self) -> dict:
        d = {
            "name": self.name,
            "statement": self.statement,
            "proof": self.proof,
            "proved": self.proved,
            "axioms": list(self.axioms),
        }
        if self.reason:
            d["reason"] = self.reason
        if self.spec_error:
            d["spec_error"] = self.spec_error
        return d


class _Abort(Exception):
    """Stop the gate with an ERROR / TIMEOUT outcome."""

    def __init__(self, outcome: Outcome, summary: str):
        super().__init__(summary)
        self.outcome = outcome
        self.summary = summary


def _rel(ctx: BackendContext, p: Path) -> str:
    return os.path.relpath(os.path.realpath(p), os.path.realpath(ctx.run_dir))


def _excerpt(text: str, limit: int = 400) -> str:
    t = redact(text.strip())
    return t if len(t) <= limit else t[: limit - 3] + "..."


class _Runner:
    """Runs Lean steps in the sandbox within one shared time budget."""

    def __init__(self, ctx: BackendContext, tc: LeanToolchain, deadline: float, raw_root: Path,
                 invocations: list[ToolInvocation], steps: list[dict]):
        self.ctx, self.tc, self.deadline, self.raw_root = ctx, tc, deadline, raw_root
        self.invocations, self.steps = invocations, steps

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def run(self, what: str, argv: list[str], *, cwd: Path, lean_path: list[Path], ro: list[Path],
            rw: list[Path], tool: str = "lean", max_output: int = MAX_LEAN_OUTPUT_BYTES) -> tuple[ProcResult, Path]:
        left = self.remaining()
        if left <= 0:
            raise _Abort(Outcome.TIMEOUT, f"Lean budget of {self.ctx.timeout(self.ctx.spec.lean.timeout_s):g}s "
                                          f"exhausted before {what}")
        env = {
            "LEAN_PATH": ":".join([self.tc.lib_dir, *(str(p) for p in lean_path)]),
            "PATH": f"{os.path.join(self.tc.prefix, 'bin')}:{DEFAULT_PATH}",
        }
        mounts = SandboxMounts(ro=[self.tc.prefix, *(str(p) for p in ro)], rw=[str(p) for p in rw])
        limits = Limits(timeout_s=left, memory_mb=LEAN_ADDRESS_SPACE_MB, max_output_bytes=max_output,
                        max_file_mb=256)
        proc = self.ctx.run(argv, limits=limits, cwd=cwd, env_extra=env, mounts=mounts)
        self.invocations.append(proc.to_invocation(tool, self.ctx.run_dir, self.ctx.log_name(STAGE, what)))
        raw = self.raw_root / f"{len(self.steps):02d}-{re.sub(r'[^A-Za-z0-9_.-]', '_', what)[:60]}.txt"
        raw.write_text(redact(proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else "")),
                       encoding="utf-8")
        self.steps.append({"step": what, "argv": list(argv), "cwd": _rel(self.ctx, cwd), "exit_code": proc.exit_code,
                           "timed_out": proc.timed_out, "duration_s": round(proc.duration_s, 3),
                           "raw": _rel(self.ctx, raw)})
        if proc.error:
            raise _Abort(Outcome.ERROR, f"{tool} could not be started for {what}: {proc.error}")
        if proc.timed_out:
            raise _Abort(Outcome.TIMEOUT, f"{what} timed out (Lean budget "
                                          f"{self.ctx.timeout(self.ctx.spec.lean.timeout_s):g}s)")
        return proc, raw


def _signal_of(proc: ProcResult) -> int | None:
    if proc.signal is not None:
        return proc.signal
    if proc.sandbox != "none" and proc.exit_code is not None and 128 < proc.exit_code < 128 + 65:
        return proc.exit_code - 128
    return None


def _check_outputs(out_dir: Path, expected: set[str], what: str) -> None:
    """The step may only have produced ``expected`` (relative paths)."""
    found = {p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if not p.is_dir()}
    extra = sorted(found - expected)
    if extra:
        raise _Abort(Outcome.ERROR, f"unexpected files were written while {what}: {extra[:10]} "
                                    "(module system / planted outputs are not accepted; output discarded)")


def _module_path(module: str) -> str:
    return module.replace(".", "/") + ".olean"


# =========================================================================== gate
def run_lean(ctx: BackendContext) -> StageResult:
    t0 = time.monotonic()
    spec = ctx.spec
    lc = spec.lean
    diagnostics: list[Diagnostic] = []
    failures: list[FailureReport] = []
    invocations: list[ToolInvocation] = []
    steps: list[dict] = []
    details: dict = {"steps": steps, "standard_axioms": list(STANDARD_AXIOMS), "sandbox": ctx.sandbox.kind}

    def result(outcome: Outcome, summary: str, status: Status | None | str = "auto") -> StageResult:
        return StageResult(
            stage=STAGE,
            outcome=outcome,
            status=outcome_to_status(STAGE, outcome) if status == "auto" else status,  # type: ignore[arg-type]
            summary=summary,
            diagnostics=diagnostics,
            failures=failures,
            invocations=invocations,
            details=details,
            duration_s=round(time.monotonic() - t0, 3),
        )

    if lc is None:
        return result(Outcome.ERROR, "gate 'lean' requires a 'lean' section in the spec")
    tc = find_lean()
    if tc is None:
        return result(Outcome.ERROR, INSTALL_HINT)
    details["lean"] = {"path": tc.lean, "prefix": tc.prefix, "version": tc.version, "version_line": tc.version_line,
                       "leanchecker": tc.leanchecker}
    try:
        layout = lean_layout(spec)
    except ValueError as exc:
        return result(Outcome.ERROR, f"unusable Lean file layout: {exc}")
    details["modules"] = {"root": layout.root or ".", "spec": layout.spec_module, "proofs": dict(layout.proof_modules),
                          "generator": layout.generator_module}
    for mod in [layout.spec_module, *layout.proof_modules.values()]:
        top = mod.split(".", 1)[0]
        if os.path.exists(os.path.join(tc.lib_dir, top + ".olean")) or os.path.isdir(os.path.join(tc.lib_dir, top)):
            return result(Outcome.ERROR, f"Lean module name {mod!r} clashes with a module of the Lean toolchain")
    snap = Path(os.path.realpath(ctx.snapshot_dir))
    if not (snap / lc.spec_file).is_file():
        return result(Outcome.ERROR, f"the approved Lean spec file {lc.spec_file!r} is missing from the snapshot")

    ok, reason = ctx.may_execute_untrusted()
    details["sandbox_reason"] = reason
    if not ok:
        return result(Outcome.SKIPPED, "Lean elaboration runs code from the editable proof files: " + reason)
    if not ctx.sandbox.active:
        details["sandbox_warning"] = reason

    out_root = Path(os.path.realpath(ctx.build_dir)) / "lean"
    raw_root = Path(os.path.realpath(ctx.raw_dir)) / "lean"
    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True)
    raw_root.mkdir(parents=True, exist_ok=True)
    runner = _Runner(ctx, tc, time.monotonic() + ctx.timeout(lc.timeout_s), raw_root, invocations, steps)
    root_arg = f"--root={layout.root or '.'}"
    theorems = [_TheoremResult(t.name, t.statement, t.proof) for t in lc.theorems]
    details["theorems"] = []
    vec: dict = {}

    try:
        # ------------------------------------------------------------ 1. trusted spec module
        spec_out = out_root / "spec"
        spec_out.mkdir()
        target = spec_out / _module_path(layout.spec_module)
        target.parent.mkdir(parents=True, exist_ok=True)
        proc, raw = runner.run(
            "compile-spec",
            [tc.lean, root_arg, "--json", f"--memory={LEAN_HEAP_MB}", "-o", str(target), lc.spec_file],
            cwd=snap, lean_path=[], ro=[snap], rw=[spec_out],
        )
        msgs, _ = parse_lean_messages(proc.stdout)
        errs = [m for m in msgs if m.get("severity") == "error"]
        if errs or proc.exit_code != 0 or not target.is_file():
            diagnostics.extend(_msg_diagnostic(m) for m in errs)
            why = _excerpt(str(errs[0].get("data"))) if errs else (
                f"terminated by signal {_signal_of(proc)}" if _signal_of(proc) else
                f"exit code {proc.exit_code}: {_excerpt(proc.stderr or proc.stdout)}")
            raise _Abort(Outcome.ERROR, f"the approved Lean spec file {lc.spec_file} does not compile ({why}); "
                                        "a human must fix the spec")
        _check_outputs(spec_out, {_module_path(layout.spec_module)}, f"compiling the spec file {lc.spec_file}")

        # ------------------------------------------------------------ 2. proof files
        file_state: dict[str, str] = {}
        good: list[tuple[str, str, Path]] = []  # (rel, module, out dir)
        for idx, rel in enumerate(lc.proofs):
            mod = layout.proof_modules[rel]
            path = snap / rel
            if not path.is_file():
                file_state[rel] = "missing"
                diagnostics.append(Diagnostic(tool="lean", rule_id="missing-proof-file", severity="error",
                                              message=f"Lean proof file {rel} does not exist",
                                              location=Location(file=rel)))
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            violations = lean_source_violations(spec, rel, text)
            if violations:
                file_state[rel] = "rejected by the Lean policy"
                for v in violations:
                    loc = Location(file=v.file, line=v.line)
                    diagnostics.append(Diagnostic(tool="leanc-policy", rule_id=v.rule, severity="error",
                                                  message=v.message, location=loc))
                    failures.append(FailureReport(
                        id=f"lean:policy:{rel}:{v.rule}", kind="policy", tool="leanc-policy", property_id=v.rule,
                        description=f"{rel}: {v.message}", location=loc,
                        explanation="Lean proof files must not contain constructs that run code at compile time, "
                        "add axioms or change how the trusted checker works; the file was not compiled, so its "
                        "theorems count as unproved.",
                    ))
                continue
            out = out_root / "proofs" / f"{idx:02d}"
            target = out / _module_path(mod)
            target.parent.mkdir(parents=True, exist_ok=True)
            proc, raw = runner.run(
                f"compile-{rel}",
                [tc.lean, root_arg, "--json", f"--memory={LEAN_HEAP_MB}", "-o", str(target), rel],
                cwd=snap, lean_path=[spec_out, *(g[2] for g in good)],
                ro=[snap, spec_out, *(g[2] for g in good)], rw=[out],
            )
            msgs, _ = parse_lean_messages(proc.stdout)
            diagnostics.extend(_msg_diagnostic(m) for m in msgs if m.get("severity") in ("error", "warning"))
            errs = [m for m in msgs if m.get("severity") == "error"]
            sig = _signal_of(proc)
            if proc.output_truncated:
                raise _Abort(Outcome.ERROR, f"Lean output for {rel} exceeded {MAX_LEAN_OUTPUT_BYTES} bytes")
            if sig is not None or (proc.exit_code != 0 and not errs):
                why = f"terminated by signal {sig}" if sig is not None else f"exit code {proc.exit_code}"
                raise _Abort(Outcome.ERROR, f"Lean failed on {rel} without a diagnostic ({why}): "
                                            f"{_excerpt(proc.stderr or proc.stdout)}")
            if errs:
                file_state[rel] = f"does not compile ({len(errs)} error(s), first: {_excerpt(str(errs[0].get('data')), 200)})"
                continue
            if not target.is_file():
                raise _Abort(Outcome.ERROR, f"Lean reported success for {rel} but wrote no .olean")
            _check_outputs(out, {_module_path(mod)}, f"compiling {rel}")
            file_state[rel] = "compiled"
            good.append((rel, mod, out))

        # ------------------------------------------------------------ 3. independent kernel replay
        replay: dict = {"tool": tc.leanchecker, "modules": {}}
        details["replay"] = replay
        if tc.leanchecker is None:
            replay["note"] = "leanchecker not found next to lean: no independent kernel replay (the gate cannot PASS)"
        else:
            kept: list[tuple[str, str, Path]] = []
            for rel, mod, out in good:
                proc, raw = runner.run(
                    f"leanchecker-{mod}", [tc.leanchecker, mod], cwd=out,
                    lean_path=[spec_out, *(g[2] for g in good)], ro=[spec_out, *(g[2] for g in good)], rw=[],
                    tool="leanchecker",
                )
                if proc.exit_code == 0:
                    replay["modules"][mod] = "ok"
                    kept.append((rel, mod, out))
                    continue
                if _signal_of(proc) is not None:
                    raise _Abort(Outcome.ERROR, f"leanchecker crashed on {mod}: {_excerpt(proc.stderr)}")
                replay["modules"][mod] = "rejected"
                file_state[rel] = "rejected by the independent kernel replay (leanchecker)"
                msg = _excerpt(proc.stderr or proc.stdout)
                diagnostics.append(Diagnostic(tool="leanchecker", rule_id="kernel-replay", severity="error",
                                              message=f"{mod}: {msg}", location=Location(file=rel)))
                failures.append(FailureReport(
                    id=f"lean:replay:{rel}", kind="policy", tool="leanchecker", property_id="kernel-replay",
                    description=f"{rel}: the compiled module {mod} does not survive an independent kernel replay",
                    location=Location(file=rel), raw_ref=_rel(ctx, raw),
                    explanation="leanchecker re-checks every declaration of the module with the Lean kernel; a "
                    "rejection means a declaration entered the environment without kernel checking.",
                ))
            good = kept
        details["proof_files"] = file_state

        # ------------------------------------------------------------ 4. trusted checker
        nonce = secrets.token_hex(8)
        check_dir = out_root / "check"
        check_dir.mkdir()
        (check_dir / CHECKER_FILE).write_text(
            render_checker(nonce, layout.spec_module, [g[1] for g in good],
                           [(t.statement, t.proof) for t in lc.theorems]),
            encoding="utf-8",
        )
        proc, raw = runner.run(
            "check-theorems",
            [tc.lean, "--root=.", "--json", f"--memory={LEAN_HEAP_MB}", CHECKER_FILE],
            cwd=check_dir, lean_path=[spec_out, *(g[2] for g in good)],
            ro=[check_dir, spec_out, *(g[2] for g in good)], rw=[],
        )
        details["checker"] = {"file": _rel(ctx, check_dir / CHECKER_FILE), "raw": _rel(ctx, raw)}
        checker_ref = _rel(ctx, raw)
        msgs, _ = parse_lean_messages(proc.stdout)
        found: dict[int, dict] = {}
        for m in msgs:
            if m.get("severity") != "information" or m.get("fileName") != CHECKER_FILE:
                continue
            try:
                payload = json.loads(str(m.get("data", "")))
            except ValueError:
                continue
            if not isinstance(payload, dict) or payload.get("leanc") != nonce:
                continue
            i = payload.get("idx")
            if not isinstance(i, int) or not 0 <= i < len(theorems):
                raise _Abort(Outcome.ERROR, "the trusted checker reported an unknown theorem index")
            if i in found:
                raise _Abort(Outcome.ERROR, f"the trusted checker reported theorem #{i} twice (forged output?)")
            found[i] = payload
        errs = [m for m in msgs if m.get("severity") == "error"]
        if proc.output_truncated:
            raise _Abort(Outcome.ERROR, "the trusted checker's output exceeded the output limit")
        if len(found) != len(theorems):
            import_errs = [m for m in errs if (_msg_location(m) or Location()).line in (None, 1)]
            if errs and len(import_errs) == len(errs) and good:
                why = _excerpt(str(errs[0].get("data")), 300)
                for t in theorems:
                    t.reason = f"the proof modules cannot be imported together: {why}"
            else:
                why = _excerpt(str(errs[0].get("data"))) if errs else f"exit code {proc.exit_code}: " + _excerpt(
                    proc.stderr or proc.stdout)
                raise _Abort(Outcome.ERROR, f"the trusted Lean checker failed ({why})")
        elif errs or proc.exit_code != 0:
            raise _Abort(Outcome.ERROR, f"the trusted Lean checker reported errors: {_excerpt(str(errs[0].get('data')) if errs else proc.stderr)}")
        failed_files = {r: s for r, s in file_state.items() if s != "compiled"}
        for i, t in enumerate(theorems):
            p = found.get(i)
            if p is None:
                continue
            status = p.get("status")
            detail = str(p.get("detail") or "")
            axioms = [str(a) for a in p.get("axioms") or []]
            missing = [str(a) for a in p.get("missing") or []]
            t.axioms = sorted(axioms)
            if status == "spec-error":
                t.spec_error = detail or "invalid statement"
            elif status == "checked":
                bad = sorted(a for a in axioms if a not in STANDARD_AXIOMS)
                if missing:
                    t.reason = f"the proof refers to constants missing from the environment: {missing[:5]}"
                elif bad:
                    what = "uses `sorry` (sorryAx)" if bad == ["sorryAx"] else f"depends on non-standard axioms {bad}"
                    t.reason = what
                else:
                    t.proved = True
            elif status == "unproved":
                t.reason = detail
                if "not defined" in detail and failed_files:
                    t.reason += " (" + "; ".join(f"{r}: {s}" for r, s in failed_files.items()) + ")"
            else:
                raise _Abort(Outcome.ERROR, f"the trusted checker returned an unknown status {status!r}")
        details["theorems"] = [t.as_dict() for t in theorems]
        spec_errors = [t for t in theorems if t.spec_error]
        if spec_errors:
            raise _Abort(Outcome.ERROR, "approved Lean statement(s) are not usable: " + "; ".join(
                f"{t.name} ({t.statement}): {t.spec_error}" for t in spec_errors) + " - a human must fix the spec")
        for t in theorems:
            if t.proved:
                continue
            failures.append(FailureReport(
                id=f"lean:unproved:{t.name}", kind="unproved", tool="lean", property_id=t.name,
                description=f"Lean theorem '{t.name}' ({t.statement}) is not proved by {t.proof}\nreason: {t.reason}",
                explanation=(
                    f"Define `{t.proof}` in an editable proof file ({', '.join(lc.proofs)}) as a theorem whose type is "
                    f"exactly `{t.statement}` from {lc.spec_file}, proved with core Lean 4 tactics only (omega, simp, "
                    "decide, induction, ...): no sorry/admit, no axioms, no native_decide."
                ),
                raw_ref=checker_ref,
            ))

        # ------------------------------------------------------------ 5. differential test vectors
        if lc.vectors is not None:
            vec = _run_vectors(ctx, tc, runner, layout, spec_out, out_root, raw_root, diagnostics, failures)
            details["vectors"] = vec
    except _Abort as ab:
        details["theorems"] = details.get("theorems") or [t.as_dict() for t in theorems]
        if vec:
            vec.pop("status_override", None)
            details["vectors"] = vec
        return result(ab.outcome, ab.summary)

    # ---------------------------------------------------------------- outcome
    proved = [t for t in theorems if t.proved]
    unproved = [t for t in theorems if not t.proved]
    axioms_used = sorted({a for t in proved for a in t.axioms})
    ver = tc.version or "?"
    thm_text = f"{len(proved)}/{len(theorems)} Lean theorem(s) about the model proved"
    vstat = vec.get("status") if lc.vectors is not None else None
    override = vec.pop("status_override", None)
    vtext = ""
    if lc.vectors is not None:
        vtext = f"; vectors: {vec.get('summary', vstat)}"
    if vstat == "fail":
        return result(Outcome.FAIL, f"the C implementation disagrees with the Lean model: {vec.get('summary')}; "
                                    f"{thm_text}", status=override or "auto")
    if vstat == "timeout":
        return result(Outcome.TIMEOUT, f"{vec.get('summary')}; {thm_text}")
    if vstat == "error":
        return result(Outcome.ERROR, f"{vec.get('summary')}; {thm_text}")
    if unproved:
        names = ", ".join(f"{t.name} ({t.reason[:80]})" for t in unproved[:6])
        return result(Outcome.INCONCLUSIVE, f"{len(unproved)} of {len(theorems)} Lean theorem(s) not proved: {names}"
                                            f"{vtext}")
    if vstat == "skipped":
        return result(Outcome.INCONCLUSIVE, f"{thm_text}{vtext}")
    if tc.leanchecker is None:
        # Without the replay a declaration added without kernel checking (environment hacking) would be
        # indistinguishable from a proof: the checker trusts the .olean it imports.
        return result(Outcome.ERROR, f"{thm_text}{vtext}, but leanchecker (independent kernel replay) was not found "
                                     f"in {os.path.join(tc.prefix, 'bin')}; refusing to PASS without it")
    where = f"sandbox: {ctx.sandbox.kind}" if ctx.sandbox.active else "UNSANDBOXED"
    replay_note = "replayed by leanchecker"
    link = (
        f"the model is linked to the C implementation only by {vec.get('count', 0)} differential test vector(s) "
        f"generated by {lc.vectors.generator} and checked by {lc.vectors.driver} (not a proof of the C code)"
        if lc.vectors is not None
        else "the model is NOT linked to the C implementation (no lean.vectors configured); "
        "nothing about the C code follows from these proofs"
    )
    details["assumptions"] = [
        (
            f"Lean 4 (v{ver}) kernel checked proofs of {len(theorems)} theorem(s) about the MODEL in {lc.spec_file} "
            f"(axioms: {', '.join(axioms_used) or 'none'}; proof modules {replay_note}); {link}"
        )
    ]
    return result(Outcome.PASS, f"Lean {ver}: {thm_text} (axioms: {', '.join(axioms_used) or 'none'}){vtext} "
                                f"({where})")


# =========================================================================== vectors
def _run_vectors(ctx: BackendContext, tc: LeanToolchain, runner: _Runner, layout: LeanLayout, spec_out: Path,
                 out_root: Path, raw_root: Path, diagnostics: list[Diagnostic],
                 failures: list[FailureReport]) -> dict:
    """Generate vectors from the model and check the C implementation on them.
    Returns ``details["vectors"]`` with ``status`` pass | fail | timeout | error | skipped."""
    spec = ctx.spec
    lv = spec.lean.vectors  # type: ignore[union-attr]
    assert lv is not None
    snap = Path(os.path.realpath(ctx.snapshot_dir))
    vec: dict = {"generator": lv.generator, "driver": lv.driver}

    def done(status: str, summary: str, **kw) -> dict:
        vec.update(status=status, summary=summary, **kw)
        return vec

    for rel, what in ((lv.generator, "generator"), (lv.driver, "driver")):
        if not (snap / rel).is_file():
            return done("error", f"the trusted vector {what} {rel!r} is missing from the snapshot")
    # -------------------------------------------------------------- generate
    try:
        proc, _raw = runner.run(
            "vectors-generate",
            [tc.lean, f"--memory={LEAN_HEAP_MB}", "--run", lv.generator],
            cwd=snap, lean_path=[spec_out], ro=[snap, spec_out], rw=[], max_output=MAX_VECTOR_BYTES,
        )
    except _Abort as ab:
        return done("timeout" if ab.outcome is Outcome.TIMEOUT else "error", ab.summary)
    if proc.output_truncated:
        return done("error", f"the vector generator printed more than {MAX_VECTOR_BYTES} bytes")
    if proc.exit_code != 0:
        return done("error", f"the trusted vector generator {lv.generator} failed (exit {proc.exit_code}): "
                             f"{_excerpt(proc.stdout + proc.stderr, 300)}")
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return done("error", f"the vector generator {lv.generator} printed no vectors")
    vdir = out_root / "vectors"
    vdir.mkdir(exist_ok=True)
    vfile = vdir / "vectors.txt"
    vfile.write_text(proc.stdout, encoding="utf-8")
    vraw = raw_root / "vectors.txt"
    vraw.write_text(proc.stdout, encoding="utf-8")
    vec.update(count=len(lines), file=_rel(ctx, vraw))

    # -------------------------------------------------------------- never execute code with forbidden constructs
    headers = {h: (snap / h).read_text(encoding="utf-8", errors="replace") for h in spec.sources.headers
               if (snap / h).is_file()}
    blocked: list[str] = []
    for rel in spec.sources.editable:
        if (snap / rel).is_file():
            vs = source_violations(spec, rel, (snap / rel).read_text(encoding="utf-8", errors="replace"),
                                   headers=headers)
            blocked += [f"{v.rule} in {rel}" + (f":{v.line}" if v.line else "") for v in vs]
    blocked += [d.message for d in san.scan_sanitizer_control(snap, spec.sources.editable)]
    if blocked:
        return done("skipped", "not executed: the editable sources contain constructs the integrity rules forbid "
                               f"({'; '.join(blocked[:3])})")
    cc, cc_version = _compiler(ctx)
    if not cc:
        return done("error", f"compiler not found (spec.compile.compiler={spec.compile.compiler!r}); install clang or "
                             "set LEANC_CLANG")

    # -------------------------------------------------------------- build
    plan = san.sanitizer_plan(spec.tests.sanitizers)
    bdir = vdir / "bin"
    bdir.mkdir(exist_ok=True)
    binary = bdir / "vector_driver"
    defines = [d if d.startswith("-D") else f"-D{d}" for d in spec.compile.defines]
    argv = [
        cc, f"-std={spec.language.standard}", *san.BASE_FLAGS, *plan.compile_flags, *spec.compile.flags,
        *defines, *ctx.include_args(), f"-I{os.path.realpath(ctx.include_dir)}", *san.DIAGNOSTIC_FLAGS,
        *spec.sources.editable, lv.driver, "-o", str(binary),
    ]
    vec["build"] = {"argv": argv, "sanitizers": list(plan.effective)}
    left = runner.remaining()
    if left <= 0:
        return done("timeout", "Lean budget exhausted before building the vector driver")
    bproc = ctx.run(argv, limits=Limits(timeout_s=min(left, ctx.timeout(spec.compile.timeout_s))), cwd=snap)
    ctx_log = bproc.to_invocation("clang", ctx.run_dir, ctx.log_name(STAGE, "vectors-build"))
    runner.invocations.append(ctx_log)
    braw = raw_root / "vectors-build.stderr.txt"
    braw.write_text(redact(bproc.stderr), encoding="utf-8")
    if bproc.error:
        return done("error", f"the vector driver build could not start: {bproc.error}")
    if bproc.timed_out:
        return done("timeout", "building the vector driver timed out")
    if bproc.exit_code != 0 or not binary.is_file():
        bdiags = san.parse_build_diagnostics(bproc.stderr, base_dir=str(snap))
        diagnostics.extend(bdiags)
        first = next((d for d in bdiags if d.severity == "error"), None)
        desc = "the editable sources do not build/link against the trusted vector driver" + (
            f": {first.message}" if first else f" (clang exit code {bproc.exit_code})")
        failures.append(FailureReport(
            id="lean:vectors:build", kind="compile", tool="clang", property_id=first.rule_id if first else None,
            description=desc, location=first.location if first else None, raw_ref=_rel(ctx, braw),
        ))
        return done("fail", desc, status_override=Status.COMPILE_ERROR)

    # -------------------------------------------------------------- run (untrusted)
    left = runner.remaining()
    if left <= 0:
        return done("timeout", "Lean budget exhausted before running the vector driver")
    rdir = vdir / "run"
    rdir.mkdir(exist_ok=True)
    symbolizer = san.find_llvm_symbolizer(cc, cc_version)
    env = san.sanitizer_env(plan, spec.tests.memory_mb, symbolizer)
    limits = Limits(
        timeout_s=left,
        cpu_s=math.ceil(left),
        memory_mb=None if plan.uses_shadow_runtime else spec.tests.memory_mb,
        max_output_bytes=spec.tests.max_output_bytes,
        max_file_mb=64,
    )
    rproc = ctx.run([str(binary)], limits=limits, cwd=rdir, env_extra=env, stdin_data=proc.stdout.encode("utf-8"),
                    mounts=SandboxMounts(ro=[str(bdir)], rw=[str(rdir)]))
    runner.invocations.append(rproc.to_invocation("lean-vectors", ctx.run_dir, ctx.log_name(STAGE, "vectors-run")))
    rraw = raw_root / "vectors-run.txt"
    rraw.write_text(redact(rproc.stdout + "\n--- stderr ---\n" + rproc.stderr), encoding="utf-8")
    raw_ref = _rel(ctx, rraw)
    vec["run"] = {"exit_code": rproc.exit_code, "raw": raw_ref, "duration_s": round(rproc.duration_s, 3)}
    if rproc.error:
        return done("error", f"could not run the vector driver: {rproc.error}")
    sig = _signal_of(rproc)
    project_files = {*spec.sources.editable, *spec.sources.headers, lv.driver}
    findings = san.parse_sanitizer_findings(rproc.stderr, project_files=project_files, base_dir=str(snap))
    mismatches = [m.group(1).strip() for m in _MISMATCH_RE.finditer(rproc.stdout + "\n" + rproc.stderr)]
    for f in findings:
        d = f.diagnostic
        d.message = f"{d.message} [Lean test vectors]"
        diagnostics.append(d)
        failures.append(FailureReport(
            id=f"lean:vectors:{d.tool}:{d.rule_id}", kind="runtime", tool=d.tool, property_id=d.rule_id,
            description=d.message, location=d.location, call_stack=f.call_stack(), raw_ref=raw_ref,
            inputs=_inputs(mismatches[0]) if mismatches else [],
            explanation=f"{d.tool} reported {d.rule_id} while the trusted driver {lv.driver} ran the vectors "
            f"generated from the Lean model; a sanitizer report is definite evidence of undefined behaviour.",
        ))
    for k, text in enumerate(mismatches[:MAX_REPORTED_MISMATCHES]):
        key = " ".join(f"{a}={b}" for a, b in _KV_RE.findall(text)) or text[:120]
        failures.append(FailureReport(
            id=f"lean:vectors:mismatch:{key}", kind="test-oracle", tool="lean-vectors", property_id="vector-mismatch",
            description=f"the C implementation disagrees with the Lean model: {text[:300]}",
            inputs=_inputs(text), raw_ref=raw_ref,
            explanation=f"{lv.driver} compared the implementation with the expected output computed by the "
            f"Lean model ({spec.lean.spec_file}) for this vector.",  # type: ignore[union-attr]
        ))
        diagnostics.append(Diagnostic(tool="lean-vectors", rule_id="vector-mismatch", severity="error",
                                      message=f"vector mismatch: {text[:300]}"))
    vec["mismatches"] = len(mismatches)
    if findings or mismatches:
        what = f"{len(mismatches)} mismatching vector(s)" + (f", first: {mismatches[0][:160]}" if mismatches else "")
        if findings:
            what += f"; {findings[0].diagnostic.tool} {findings[0].diagnostic.rule_id}"
        return done("fail", what)
    if rproc.output_truncated:
        failures.append(FailureReport(id="lean:vectors:output-limit", kind="test-oracle", tool="lean-vectors",
                                      property_id="output-limit-exceeded", raw_ref=raw_ref,
                                      description="the vector driver exceeded tests.max_output_bytes"))
        return done("fail", "the vector driver exceeded the output limit")
    if rproc.timed_out or sig == _signal.SIGXCPU:
        return done("timeout", f"the vector driver did not finish within {left:.0f}s")
    if rproc.exit_code != 0:
        got = f"terminated by signal {sig}" if sig is not None else f"exit code {rproc.exit_code}"
        failures.append(FailureReport(
            id="lean:vectors:exit-code", kind="test-oracle", tool="lean-vectors", property_id="exit-code",
            description=f"the vector driver failed ({got}) without reporting a mismatching vector", raw_ref=raw_ref,
            explanation="See the raw output of the vector run.",
        ))
        return done("fail", f"the vector driver failed ({got})")
    checked = _CHECKED_RE.findall(rproc.stdout)
    if len(checked) != 1 or not checked[0].isdigit():
        return done("error", f"the vector driver must print exactly one '{VECTOR_CHECKED_TAG} <n>' line "
                             f"(found {len(checked)}); the vector run is not conclusive")
    n = int(checked[0])
    if n != len(lines):
        return done("error", f"the vector driver checked {n} of {len(lines)} vector(s); the vector run is not "
                             "conclusive")
    return done("pass", f"{n}/{len(lines)} test vector(s) agree with the model")


def _compiler(ctx: BackendContext) -> tuple[str | None, str | None]:
    """The driver for the vector build: clang++ for C++ specs, clang for C."""
    return compiler(ctx), compiler_version(ctx)


def _inputs(text: str) -> list[CounterexampleInput]:
    pairs = _KV_RE.findall(text)
    if not pairs:
        return [CounterexampleInput(name="vector", value=text[:300])]
    return [CounterexampleInput(name=k, value=v[:200]) for k, v in pairs[:20]]
