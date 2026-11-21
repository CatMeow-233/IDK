from __future__ import annotations

import os
import secrets
import shutil
import time
from pathlib import Path
from typing import NamedTuple

from leanc.backends.lean.checker import CHECKER_FILE, checker_reports, render_checker
from leanc.backends.lean.messages import (
    errors_of,
    message_diagnostic,
    message_location,
    parse_lean_messages,
)
from leanc.backends.lean.steps import (
    LEAN_HEAP_MB,
    MAX_LEAN_OUTPUT_BYTES,
    STAGE,
    GateAbort,
    StepRunner,
    check_outputs,
    excerpt,
    module_olean_path,
    run_relative,
)
from leanc.backends.lean.theorems import (
    STANDARD_AXIOMS,
    TheoremResult,
    settle_theorem,
    unproved_theorem_failure,
)
from leanc.backends.lean.vectors import VectorCheck
from leanc.backends.termination import terminating_signal
from leanc.foundation.models import (
    Diagnostic,
    FailureReport,
    Location,
    StageResult,
    ToolInvocation,
)
from leanc.foundation.status import Outcome, Status, outcome_to_status
from leanc.rules.lean_rules import LeanLayout, lean_layout, lean_source_violations
from leanc.toolchain.context import BackendContext
from leanc.toolchain.lean import INSTALL_HINT, LeanToolchain, find_lean

FILE_COMPILED = "compiled"
FILE_MISSING = "missing"
FILE_REJECTED_BY_POLICY = "rejected by the Lean policy"
FILE_REJECTED_BY_REPLAY = "rejected by the independent kernel replay (leanchecker)"
REPLAY_OK = "ok"
REPLAY_REJECTED = "rejected"
DIAGNOSED_SEVERITIES = ("error", "warning")
IMPORT_ERROR_LINES = (None, 1)
NONCE_BYTES = 8
MAX_FIRST_ERROR_CHARS = 200
MAX_IMPORT_ERROR_CHARS = 300
MAX_LISTED_UNPROVED = 6
MAX_LISTED_REASON_CHARS = 80
POLICY_EXPLANATION = (
    "Lean proof files must not contain constructs that run code at compile time, "
    "add axioms or change how the trusted checker works; the file was not compiled, so its "
    "theorems count as unproved."
)
REPLAY_EXPLANATION = (
    "leanchecker re-checks every declaration of the module with the Lean kernel; a "
    "rejection means a declaration entered the environment without kernel checking."
)


class CompiledProof(NamedTuple):
    rel: str
    module: str
    out_dir: Path


def _toolchain_module_clash(tc: LeanToolchain, layout: LeanLayout) -> str | None:
    for module in [layout.spec_module, *layout.proof_modules.values()]:
        top = module.split(".", 1)[0]
        if os.path.exists(os.path.join(tc.lib_dir, top + ".olean")) or os.path.isdir(os.path.join(tc.lib_dir, top)):
            return module
    return None


def _is_import_error(message: dict) -> bool:
    return (message_location(message) or Location()).line in IMPORT_ERROR_LINES


def _compile_failure_reason(proc, errors: list[dict]) -> str:
    if errors:
        return excerpt(str(errors[0].get("data")))
    if terminating_signal(proc):
        return f"terminated by signal {terminating_signal(proc)}"
    return f"exit code {proc.exit_code}: {excerpt(proc.stderr or proc.stdout)}"


class _LeanRun:
    def __init__(self, ctx: BackendContext):
        self.started = time.monotonic()
        self.ctx = ctx
        self.spec = ctx.spec
        self.lean = ctx.spec.lean
        self.diagnostics: list[Diagnostic] = []
        self.failures: list[FailureReport] = []
        self.invocations: list[ToolInvocation] = []
        self.steps: list[dict] = []
        self.details: dict = {
            "steps": self.steps,
            "standard_axioms": list(STANDARD_AXIOMS),
            "sandbox": ctx.sandbox.kind,
        }
        self.snapshot = Path(os.path.realpath(ctx.snapshot_dir))
        self.out_root = Path(os.path.realpath(ctx.build_dir)) / "lean"
        self.raw_root = Path(os.path.realpath(ctx.raw_dir)) / "lean"
        self.theorems: list[TheoremResult] = []
        self.file_state: dict[str, str] = {}
        self.vectors: dict = {}
        self.tc: LeanToolchain
        self.layout: LeanLayout
        self.runner: StepRunner
        self.root_arg = ""

    def result(self, outcome: Outcome, summary: str, status_override: Status | None = None) -> StageResult:
        return StageResult(
            stage=STAGE,
            outcome=outcome,
            status=status_override or outcome_to_status(STAGE, outcome),
            summary=summary,
            diagnostics=self.diagnostics,
            failures=self.failures,
            invocations=self.invocations,
            details=self.details,
            duration_s=round(time.monotonic() - self.started, 3),
        )

    def execute(self) -> StageResult:
        refused = self._preflight()
        if refused is not None:
            return refused
        self._prepare_outputs()
        try:
            self._prove()
        except GateAbort as abort:
            return self._aborted(abort)
        return self._outcome()

    def _preflight(self) -> StageResult | None:
        ctx, lean = self.ctx, self.lean
        if lean is None:
            return self.result(Outcome.ERROR, "gate 'lean' requires a 'lean' section in the spec")
        tc = find_lean()
        if tc is None:
            return self.result(Outcome.ERROR, INSTALL_HINT)
        self.tc = tc
        self.details["lean"] = {
            "path": tc.lean,
            "prefix": tc.prefix,
            "version": tc.version,
            "version_line": tc.version_line,
            "leanchecker": tc.leanchecker,
        }
        try:
            layout = lean_layout(self.spec)
        except ValueError as exc:
            return self.result(Outcome.ERROR, f"unusable Lean file layout: {exc}")
        self.layout = layout
        self.details["modules"] = {
            "root": layout.root or ".",
            "spec": layout.spec_module,
            "proofs": dict(layout.proof_modules),
            "generator": layout.generator_module,
        }
        clash = _toolchain_module_clash(tc, layout)
        if clash is not None:
            return self.result(Outcome.ERROR, f"Lean module name {clash!r} clashes with a module of the Lean toolchain")
        if not (self.snapshot / lean.spec_file).is_file():
            return self.result(
                Outcome.ERROR, f"the approved Lean spec file {lean.spec_file!r} is missing from the snapshot"
            )
        allowed, reason = ctx.may_execute_untrusted()
        self.details["sandbox_reason"] = reason
        if not allowed:
            return self.result(Outcome.SKIPPED, "Lean elaboration runs code from the editable proof files: " + reason)
        if not ctx.sandbox.active:
            self.details["sandbox_warning"] = reason
        return None

    def _prepare_outputs(self) -> None:
        if self.out_root.exists():
            shutil.rmtree(self.out_root)
        self.out_root.mkdir(parents=True)
        self.raw_root.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.ctx.timeout(self.lean.timeout_s)
        self.runner = StepRunner(self.ctx, self.tc, deadline, self.raw_root, self.invocations, self.steps)
        self.root_arg = f"--root={self.layout.root or '.'}"
        self.theorems = [TheoremResult(t.name, t.statement, t.proof) for t in self.lean.theorems]
        self.details["theorems"] = []

    def _prove(self) -> None:
        spec_out = self._compile_spec()
        compiled = self._compile_proofs(spec_out)
        compiled = self._replay_in_kernel(compiled, spec_out)
        self.details["proof_files"] = self.file_state
        self._check_theorems(compiled, spec_out)
        if self.lean.vectors is not None:
            dirs = (spec_out, self.out_root, self.raw_root)
            check = VectorCheck(
                self.ctx, self.tc, self.runner, self.lean, self.lean.vectors, dirs, self.diagnostics, self.failures
            )
            self.vectors = check.run()
            self.details["vectors"] = self.vectors

    def _compile_command(self, output: Path, source: str) -> list[str]:
        return [self.tc.lean, self.root_arg, "--json", f"--memory={LEAN_HEAP_MB}", "-o", str(output), source]

    def _compile_spec(self) -> Path:
        lean = self.lean
        spec_module_path = module_olean_path(self.layout.spec_module)
        spec_out = self.out_root / "spec"
        spec_out.mkdir()
        target = spec_out / spec_module_path
        target.parent.mkdir(parents=True, exist_ok=True)
        proc, _ = self.runner.run(
            "compile-spec",
            self._compile_command(target, lean.spec_file),
            cwd=self.snapshot,
            lean_path=[],
            ro=[self.snapshot],
            rw=[spec_out],
        )
        messages, _ = parse_lean_messages(proc.stdout)
        errors = errors_of(messages)
        if errors or proc.exit_code != 0 or not target.is_file():
            self.diagnostics.extend(message_diagnostic(m) for m in errors)
            raise GateAbort(
                Outcome.ERROR,
                f"the approved Lean spec file {lean.spec_file} does not compile "
                f"({_compile_failure_reason(proc, errors)}); a human must fix the spec",
            )
        check_outputs(spec_out, {spec_module_path}, f"compiling the spec file {lean.spec_file}")
        return spec_out

    def _report_missing_proof_file(self, rel: str) -> None:
        self.file_state[rel] = FILE_MISSING
        self.diagnostics.append(
            Diagnostic(
                tool="lean",
                rule_id="missing-proof-file",
                severity="error",
                message=f"Lean proof file {rel} does not exist",
                location=Location(file=rel),
            )
        )

    def _report_policy_violations(self, rel: str, violations: list) -> None:
        self.file_state[rel] = FILE_REJECTED_BY_POLICY
        for violation in violations:
            location = Location(file=violation.file, line=violation.line)
            self.diagnostics.append(
                Diagnostic(
                    tool="leanc-policy",
                    rule_id=violation.rule,
                    severity="error",
                    message=violation.message,
                    location=location,
                )
            )
            self.failures.append(
                FailureReport(
                    id=f"lean:policy:{rel}:{violation.rule}",
                    kind="policy",
                    tool="leanc-policy",
                    property_id=violation.rule,
                    description=f"{rel}: {violation.message}",
                    location=location,
                    explanation=POLICY_EXPLANATION,
                )
            )

    def _compile_proofs(self, spec_out: Path) -> list[CompiledProof]:
        compiled: list[CompiledProof] = []
        for index, rel in enumerate(self.lean.proofs):
            path = self.snapshot / rel
            if not path.is_file():
                self._report_missing_proof_file(rel)
                continue
            violations = lean_source_violations(self.spec, rel, path.read_text(encoding="utf-8", errors="replace"))
            if violations:
                self._report_policy_violations(rel, violations)
                continue
            proof = self._compile_proof(index, rel, spec_out, compiled)
            if proof is not None:
                compiled.append(proof)
        return compiled

    def _compile_proof(
        self, index: int, rel: str, spec_out: Path, compiled: list[CompiledProof]
    ) -> CompiledProof | None:
        module = self.layout.proof_modules[rel]
        out = self.out_root / "proofs" / f"{index:02d}"
        target = out / module_olean_path(module)
        target.parent.mkdir(parents=True, exist_ok=True)
        earlier = [proof.out_dir for proof in compiled]
        proc, _ = self.runner.run(
            f"compile-{rel}",
            self._compile_command(target, rel),
            cwd=self.snapshot,
            lean_path=[spec_out, *earlier],
            ro=[self.snapshot, spec_out, *earlier],
            rw=[out],
        )
        messages, _ = parse_lean_messages(proc.stdout)
        self.diagnostics.extend(message_diagnostic(m) for m in messages if m.get("severity") in DIAGNOSED_SEVERITIES)
        errors = errors_of(messages)
        sig = terminating_signal(proc)
        if proc.output_truncated:
            raise GateAbort(Outcome.ERROR, f"Lean output for {rel} exceeded {MAX_LEAN_OUTPUT_BYTES} bytes")
        if sig is not None or (proc.exit_code != 0 and not errors):
            why = f"terminated by signal {sig}" if sig is not None else f"exit code {proc.exit_code}"
            raise GateAbort(
                Outcome.ERROR,
                f"Lean failed on {rel} without a diagnostic ({why}): {excerpt(proc.stderr or proc.stdout)}",
            )
        if errors:
            first = excerpt(str(errors[0].get("data")), MAX_FIRST_ERROR_CHARS)
            self.file_state[rel] = f"does not compile ({len(errors)} error(s), first: {first})"
            return None
        if not target.is_file():
            raise GateAbort(Outcome.ERROR, f"Lean reported success for {rel} but wrote no .olean")
        check_outputs(out, {module_olean_path(module)}, f"compiling {rel}")
        self.file_state[rel] = FILE_COMPILED
        return CompiledProof(rel, module, out)

    def _report_replay_rejection(self, proof: CompiledProof, proc, raw: Path) -> None:
        self.file_state[proof.rel] = FILE_REJECTED_BY_REPLAY
        message = excerpt(proc.stderr or proc.stdout)
        self.diagnostics.append(
            Diagnostic(
                tool="leanchecker",
                rule_id="kernel-replay",
                severity="error",
                message=f"{proof.module}: {message}",
                location=Location(file=proof.rel),
            )
        )
        self.failures.append(
            FailureReport(
                id=f"lean:replay:{proof.rel}",
                kind="policy",
                tool="leanchecker",
                property_id="kernel-replay",
                description=f"{proof.rel}: the compiled module {proof.module} does not survive an independent "
                "kernel replay",
                location=Location(file=proof.rel),
                raw_ref=run_relative(self.ctx, raw),
                explanation=REPLAY_EXPLANATION,
            )
        )

    def _replay_in_kernel(self, compiled: list[CompiledProof], spec_out: Path) -> list[CompiledProof]:
        leanchecker = self.tc.leanchecker
        replay: dict = {"tool": leanchecker, "modules": {}}
        self.details["replay"] = replay
        if leanchecker is None:
            replay["note"] = "leanchecker not found next to lean: no independent kernel replay (the gate cannot PASS)"
            return compiled
        outputs = [spec_out, *(proof.out_dir for proof in compiled)]
        kept: list[CompiledProof] = []
        for proof in compiled:
            proc, raw = self.runner.run(
                f"leanchecker-{proof.module}",
                [leanchecker, proof.module],
                cwd=proof.out_dir,
                lean_path=outputs,
                ro=outputs,
                rw=[],
                tool="leanchecker",
            )
            if proc.exit_code == 0:
                replay["modules"][proof.module] = REPLAY_OK
                kept.append(proof)
                continue
            if terminating_signal(proc) is not None:
                raise GateAbort(Outcome.ERROR, f"leanchecker crashed on {proof.module}: {excerpt(proc.stderr)}")
            replay["modules"][proof.module] = REPLAY_REJECTED
            self._report_replay_rejection(proof, proc, raw)
        return kept

    def _write_checker(self, nonce: str, compiled: list[CompiledProof]) -> Path:
        check_dir = self.out_root / "check"
        check_dir.mkdir()
        source = render_checker(
            nonce,
            self.layout.spec_module,
            [proof.module for proof in compiled],
            [(t.statement, t.proof) for t in self.lean.theorems],
        )
        (check_dir / CHECKER_FILE).write_text(source, encoding="utf-8")
        return check_dir

    def _check_theorems(self, compiled: list[CompiledProof], spec_out: Path) -> None:
        nonce = secrets.token_hex(NONCE_BYTES)
        check_dir = self._write_checker(nonce, compiled)
        outputs = [spec_out, *(proof.out_dir for proof in compiled)]
        proc, raw = self.runner.run(
            "check-theorems",
            [self.tc.lean, "--root=.", "--json", f"--memory={LEAN_HEAP_MB}", CHECKER_FILE],
            cwd=check_dir,
            lean_path=outputs,
            ro=[check_dir, *outputs],
            rw=[],
        )
        checker_ref = run_relative(self.ctx, raw)
        self.details["checker"] = {"file": run_relative(self.ctx, check_dir / CHECKER_FILE), "raw": checker_ref}
        messages, _ = parse_lean_messages(proc.stdout)
        reports = checker_reports(messages, nonce, len(self.theorems))
        self._judge_checker_run(proc, errors_of(messages), reports, compiled)
        failed_files = {rel: state for rel, state in self.file_state.items() if state != FILE_COMPILED}
        for index, theorem in enumerate(self.theorems):
            report = reports.get(index)
            if report is not None:
                settle_theorem(theorem, report, failed_files)
        self.details["theorems"] = [t.as_dict() for t in self.theorems]
        self._refuse_unusable_statements()
        self.failures += [
            unproved_theorem_failure(t, self.lean, checker_ref) for t in self.theorems if not t.proved
        ]

    def _judge_checker_run(self, proc, errors: list[dict], reports: dict[int, dict], compiled: list) -> None:
        if proc.output_truncated:
            raise GateAbort(Outcome.ERROR, "the trusted checker's output exceeded the output limit")
        if len(reports) != len(self.theorems):
            self._explain_missing_reports(proc, errors, compiled)
        elif errors or proc.exit_code != 0:
            shown = excerpt(str(errors[0].get("data")) if errors else proc.stderr)
            raise GateAbort(Outcome.ERROR, f"the trusted Lean checker reported errors: {shown}")

    def _explain_missing_reports(self, proc, errors: list[dict], compiled: list) -> None:
        import_errors = [m for m in errors if _is_import_error(m)]
        if errors and len(import_errors) == len(errors) and compiled:
            why = excerpt(str(errors[0].get("data")), MAX_IMPORT_ERROR_CHARS)
            for theorem in self.theorems:
                theorem.reason = f"the proof modules cannot be imported together: {why}"
            return
        if errors:
            why = excerpt(str(errors[0].get("data")))
        else:
            why = f"exit code {proc.exit_code}: " + excerpt(proc.stderr or proc.stdout)
        raise GateAbort(Outcome.ERROR, f"the trusted Lean checker failed ({why})")

    def _refuse_unusable_statements(self) -> None:
        spec_errors = [t for t in self.theorems if t.spec_error]
        if spec_errors:
            listed = "; ".join(f"{t.name} ({t.statement}): {t.spec_error}" for t in spec_errors)
            raise GateAbort(
                Outcome.ERROR, f"approved Lean statement(s) are not usable: {listed} - a human must fix the spec"
            )

    def _aborted(self, abort: GateAbort) -> StageResult:
        self.details["theorems"] = self.details.get("theorems") or [t.as_dict() for t in self.theorems]
        if self.vectors:
            self.vectors.pop("status_override", None)
            self.details["vectors"] = self.vectors
        return self.result(abort.outcome, abort.summary)

    def _outcome(self) -> StageResult:
        lean, tc, theorems, vectors = self.lean, self.tc, self.theorems, self.vectors
        proved = [t for t in theorems if t.proved]
        unproved = [t for t in theorems if not t.proved]
        axioms_used = sorted({axiom for t in proved for axiom in t.axioms})
        theorem_text = f"{len(proved)}/{len(theorems)} Lean theorem(s) about the model proved"
        vector_status = vectors.get("status") if lean.vectors is not None else None
        status_override = vectors.pop("status_override", None)
        vector_text = f"; vectors: {vectors.get('summary', vector_status)}" if lean.vectors is not None else ""
        if vector_status == "fail":
            return self.result(
                Outcome.FAIL,
                f"the C implementation disagrees with the Lean model: {vectors.get('summary')}; {theorem_text}",
                status_override=status_override,
            )
        if vector_status == "timeout":
            return self.result(Outcome.TIMEOUT, f"{vectors.get('summary')}; {theorem_text}")
        if vector_status == "error":
            return self.result(Outcome.ERROR, f"{vectors.get('summary')}; {theorem_text}")
        if unproved:
            listed = unproved[:MAX_LISTED_UNPROVED]
            names = ", ".join(f"{t.name} ({t.reason[:MAX_LISTED_REASON_CHARS]})" for t in listed)
            return self.result(
                Outcome.INCONCLUSIVE,
                f"{len(unproved)} of {len(theorems)} Lean theorem(s) not proved: {names}{vector_text}",
            )
        if vector_status == "skipped":
            return self.result(Outcome.INCONCLUSIVE, f"{theorem_text}{vector_text}")
        if tc.leanchecker is None:
            return self._refuse_pass_without_kernel_replay(f"{theorem_text}{vector_text}")
        version = tc.version or "?"
        self.details["assumptions"] = [self._model_scope(version, axioms_used)]
        where = f"sandbox: {self.ctx.sandbox.kind}" if self.ctx.sandbox.active else "UNSANDBOXED"
        return self.result(
            Outcome.PASS,
            f"Lean {version}: {theorem_text} (axioms: {', '.join(axioms_used) or 'none'}){vector_text} ({where})",
        )

    def _refuse_pass_without_kernel_replay(self, proved_text: str) -> StageResult:
        return self.result(
            Outcome.ERROR,
            f"{proved_text}, but leanchecker (independent kernel replay) was not found "
            f"in {os.path.join(self.tc.prefix, 'bin')}; refusing to PASS without it",
        )

    def _model_scope(self, version: str, axioms_used: list[str]) -> str:
        lean = self.lean
        if lean.vectors is not None:
            link = (
                f"the model is linked to the C implementation only by {self.vectors.get('count', 0)} differential "
                f"test vector(s) generated by {lean.vectors.generator} and checked by {lean.vectors.driver} (not a "
                "proof of the C code)"
            )
        else:
            link = (
                "the model is NOT linked to the C implementation (no lean.vectors configured); "
                "nothing about the C code follows from these proofs"
            )
        return (
            f"Lean 4 (v{version}) kernel checked proofs of {len(self.theorems)} theorem(s) about the MODEL in "
            f"{lean.spec_file} (axioms: {', '.join(axioms_used) or 'none'}; proof modules replayed by leanchecker); "
            f"{link}"
        )


def run_lean(ctx: BackendContext) -> StageResult:
    return _LeanRun(ctx).execute()
