from __future__ import annotations

import math
import os
import re
import signal as _signal
from pathlib import Path
from typing import Literal

from leanc.backends.lean.steps import (
    LEAN_HEAP_MB,
    STAGE,
    STDERR_SEPARATOR,
    GateAbort,
    StepRunner,
    excerpt,
    run_relative,
)
from leanc.backends.sanitizer import (
    BASE_FLAGS,
    DIAGNOSTIC_FLAGS,
    SanitizerFinding,
    SanitizerPlan,
    find_llvm_symbolizer,
    parse_build_diagnostics,
    parse_sanitizer_findings,
    sanitizer_env,
    sanitizer_plan,
    scan_sanitizer_control,
)
from leanc.backends.termination import untrusted_program_terminating_signal
from leanc.foundation.models import CounterexampleInput, Diagnostic, FailureReport
from leanc.foundation.redact import redact
from leanc.foundation.runner import Limits, ProcResult
from leanc.foundation.sandbox import SandboxMounts
from leanc.foundation.status import Outcome, Status
from leanc.rules.source import source_violations
from leanc.specs.model import LeanConfig, LeanVectors
from leanc.toolchain.context import BackendContext
from leanc.toolchain.cxx import compiler, compiler_version
from leanc.toolchain.lean import LeanToolchain

VECTOR_MISMATCH_TAG = "LEANC-VECTOR-MISMATCH"
VECTOR_CHECKED_TAG = "LEANC-VECTORS-CHECKED"
VECTOR_COMMENT_PREFIX = "#"
MAX_VECTOR_BYTES = 8 * 1024 * 1024
MAX_REPORTED_MISMATCHES = 20
DRIVER_MAX_FILE_MB = 64
SANITIZER_FINDING_SUFFIX = " [Lean test vectors]"
MAX_BLOCKED_LISTED = 3
MAX_VECTOR_INPUTS = 20
MAX_INPUT_VALUE_CHARS = 200
MAX_VECTOR_TEXT_CHARS = 300
MAX_MISMATCH_KEY_CHARS = 120
MAX_FIRST_MISMATCH_CHARS = 160
MAX_GENERATOR_FAILURE_CHARS = 300

VectorStatus = Literal["pass", "fail", "timeout", "error", "skipped"]

_MISMATCH_RE = re.compile(rf"^\s*{VECTOR_MISMATCH_TAG}\b(.*)$", re.MULTILINE)
MAX_VECTOR_COUNT_DIGITS = 18
_CHECKED_RE = re.compile(rf"^\s*{VECTOR_CHECKED_TAG}\s+(\S+)\s*$", re.MULTILINE)
_KEY_VALUE_RE = re.compile(r"([A-Za-z_][\w.\-]*)=(\S*)")


def vector_inputs(text: str) -> list[CounterexampleInput]:
    pairs = _KEY_VALUE_RE.findall(text)
    if not pairs:
        return [CounterexampleInput(name="vector", value=text[:MAX_VECTOR_TEXT_CHARS])]
    return [
        CounterexampleInput(name=key, value=value[:MAX_INPUT_VALUE_CHARS]) for key, value in pairs[:MAX_VECTOR_INPUTS]
    ]


def _is_vector_line(line: str) -> bool:
    return bool(line.strip()) and not line.lstrip().startswith(VECTOR_COMMENT_PREFIX)



def _is_vector_count(token: str) -> bool:
    return token.isascii() and token.isdigit() and len(token) <= MAX_VECTOR_COUNT_DIGITS

class VectorCheck:
    def __init__(
        self,
        ctx: BackendContext,
        tc: LeanToolchain,
        runner: StepRunner,
        lean: LeanConfig,
        vectors: LeanVectors,
        dirs: tuple[Path, Path, Path],
        diagnostics: list[Diagnostic],
        failures: list[FailureReport],
    ):
        self.ctx, self.tc, self.runner, self.lean, self.vectors = ctx, tc, runner, lean, vectors
        self.spec_out, self.out_root, self.raw_root = dirs
        self.diagnostics, self.failures = diagnostics, failures
        self.spec = ctx.spec
        self.snapshot = Path(os.path.realpath(ctx.snapshot_dir))
        self.details: dict = {"generator": vectors.generator, "driver": vectors.driver}
        self.generated = ""
        self.vector_count = 0
        self.vector_dir = self.out_root / "vectors"
        self.cc: str | None = None
        self.cc_version: str | None = None
        self.plan: SanitizerPlan
        self.bin_dir = self.vector_dir / "bin"
        self.binary = self.bin_dir / "vector_driver"
        self.run_budget = 0.0
        self.raw_ref = ""
        self.driver_run: ProcResult

    def _done(self, status: VectorStatus, summary: str, **extra) -> dict:
        self.details.update(status=status, summary=summary, **extra)
        return self.details

    def run(self) -> dict:
        steps = (
            self._require_trusted_files,
            self._generate,
            self._refuse_forbidden_sources,
            self._find_compiler,
            self._build_driver,
            self._run_driver,
        )
        for step in steps:
            finished = step()
            if finished is not None:
                return finished
        return self._judge_driver_run()

    def _require_trusted_files(self) -> dict | None:
        for rel, role in ((self.vectors.generator, "generator"), (self.vectors.driver, "driver")):
            if not (self.snapshot / rel).is_file():
                return self._done("error", f"the trusted vector {role} {rel!r} is missing from the snapshot")
        return None

    def _generate(self) -> dict | None:
        generator = self.vectors.generator
        try:
            proc, _ = self.runner.run(
                "vectors-generate",
                [self.tc.lean, f"--memory={LEAN_HEAP_MB}", "--run", generator],
                cwd=self.snapshot,
                lean_path=[self.spec_out],
                ro=[self.snapshot, self.spec_out],
                rw=[],
                max_output=MAX_VECTOR_BYTES,
            )
        except GateAbort as abort:
            return self._done("timeout" if abort.outcome is Outcome.TIMEOUT else "error", abort.summary)
        if proc.output_truncated:
            return self._done("error", f"the vector generator printed more than {MAX_VECTOR_BYTES} bytes")
        if proc.exit_code != 0:
            return self._done(
                "error",
                f"the trusted vector generator {generator} failed (exit {proc.exit_code}): "
                f"{excerpt(proc.stdout + proc.stderr, MAX_GENERATOR_FAILURE_CHARS)}",
            )
        self.vector_count = len([line for line in proc.stdout.splitlines() if _is_vector_line(line)])
        if not self.vector_count:
            return self._done("error", f"the vector generator {generator} printed no vectors")
        self.generated = proc.stdout
        self.vector_dir.mkdir(exist_ok=True)
        (self.vector_dir / "vectors.txt").write_text(proc.stdout, encoding="utf-8")
        kept = self.raw_root / "vectors.txt"
        kept.write_text(proc.stdout, encoding="utf-8")
        self.details.update(count=self.vector_count, file=run_relative(self.ctx, kept))
        return None

    def _forbidden_constructs(self) -> list[str]:
        spec, snapshot = self.spec, self.snapshot
        headers = {
            header: (snapshot / header).read_text(encoding="utf-8", errors="replace")
            for header in spec.sources.headers
            if (snapshot / header).is_file()
        }
        blocked: list[str] = []
        for rel in spec.sources.editable:
            if (snapshot / rel).is_file():
                text = (snapshot / rel).read_text(encoding="utf-8", errors="replace")
                violations = source_violations(spec, rel, text, headers=headers)
                blocked += [f"{v.rule} in {rel}" + (f":{v.line}" if v.line else "") for v in violations]
        blocked += [diagnostic.message for diagnostic in scan_sanitizer_control(snapshot, spec.sources.editable)]
        return blocked

    def _refuse_forbidden_sources(self) -> dict | None:
        blocked = self._forbidden_constructs()
        if not blocked:
            return None
        return self._done(
            "skipped",
            "not executed: the editable sources contain constructs the integrity rules forbid "
            f"({'; '.join(blocked[:MAX_BLOCKED_LISTED])})",
        )

    def _find_compiler(self) -> dict | None:
        self.cc, self.cc_version = compiler(self.ctx), compiler_version(self.ctx)
        if self.cc:
            return None
        return self._done(
            "error",
            f"compiler not found (spec.compile.compiler={self.spec.compile.compiler!r}); install clang or "
            "set LEANC_CLANG",
        )

    def _build_argv(self) -> list[str]:
        spec, ctx = self.spec, self.ctx
        defines = [define if define.startswith("-D") else f"-D{define}" for define in spec.compile.defines]
        return [
            self.cc, f"-std={spec.language.standard}", *BASE_FLAGS, *self.plan.compile_flags, *spec.compile.flags,
            *defines, *ctx.include_args(), f"-I{os.path.realpath(ctx.include_dir)}", *DIAGNOSTIC_FLAGS,
            *spec.sources.editable, self.vectors.driver, "-o", str(self.binary),
        ]

    def _build_failed(self, build: ProcResult, build_log: Path) -> dict:
        build_diagnostics = parse_build_diagnostics(build.stderr, base_dir=str(self.snapshot))
        self.diagnostics.extend(build_diagnostics)
        first = next((d for d in build_diagnostics if d.severity == "error"), None)
        description = "the editable sources do not build/link against the trusted vector driver" + (
            f": {first.message}" if first else f" (clang exit code {build.exit_code})"
        )
        self.failures.append(
            FailureReport(
                id="lean:vectors:build",
                kind="compile",
                tool="clang",
                property_id=first.rule_id if first else None,
                description=description,
                location=first.location if first else None,
                raw_ref=run_relative(self.ctx, build_log),
            )
        )
        return self._done("fail", description, status_override=Status.COMPILE_ERROR)

    def _build_driver(self) -> dict | None:
        ctx = self.ctx
        self.plan = sanitizer_plan(self.spec.tests.sanitizers)
        self.bin_dir.mkdir(exist_ok=True)
        argv = self._build_argv()
        self.details["build"] = {"argv": argv, "sanitizers": list(self.plan.effective)}
        left = self.runner.remaining()
        if left <= 0:
            return self._done("timeout", "Lean budget exhausted before building the vector driver")
        limits = Limits(timeout_s=min(left, ctx.timeout(self.spec.compile.timeout_s)))
        build = ctx.run(argv, limits=limits, cwd=self.snapshot)
        self.runner.invocations.append(build.to_invocation("clang", ctx.run_dir, ctx.log_name(STAGE, "vectors-build")))
        build_log = self.raw_root / "vectors-build.stderr.txt"
        build_log.write_text(redact(build.stderr), encoding="utf-8")
        if build.error:
            return self._done("error", f"the vector driver build could not start: {build.error}")
        if build.timed_out:
            return self._done("timeout", "building the vector driver timed out")
        if build.exit_code != 0 or not self.binary.is_file():
            return self._build_failed(build, build_log)
        return None

    def _run_driver(self) -> dict | None:
        ctx, spec = self.ctx, self.spec
        left = self.runner.remaining()
        if left <= 0:
            return self._done("timeout", "Lean budget exhausted before running the vector driver")
        self.run_budget = left
        run_dir = self.vector_dir / "run"
        run_dir.mkdir(exist_ok=True)
        symbolizer = find_llvm_symbolizer(self.cc, self.cc_version)
        env = sanitizer_env(self.plan, spec.tests.memory_mb, symbolizer)
        limits = Limits(
            timeout_s=left,
            cpu_s=math.ceil(left),
            memory_mb=None if self.plan.uses_shadow_runtime else spec.tests.memory_mb,
            max_output_bytes=spec.tests.max_output_bytes,
            max_file_mb=DRIVER_MAX_FILE_MB,
        )
        proc = ctx.run(
            [str(self.binary)],
            limits=limits,
            cwd=run_dir,
            env_extra=env,
            stdin_data=self.generated.encode("utf-8"),
            mounts=SandboxMounts(ro=[str(self.bin_dir)], rw=[str(run_dir)]),
        )
        self.runner.invocations.append(
            proc.to_invocation("lean-vectors", ctx.run_dir, ctx.log_name(STAGE, "vectors-run"))
        )
        run_log = self.raw_root / "vectors-run.txt"
        run_log.write_text(redact(proc.stdout + STDERR_SEPARATOR + proc.stderr), encoding="utf-8")
        self.raw_ref = run_relative(ctx, run_log)
        self.details["run"] = {
            "exit_code": proc.exit_code,
            "raw": self.raw_ref,
            "duration_s": round(proc.duration_s, 3),
        }
        self.driver_run = proc
        if proc.error:
            return self._done("error", f"could not run the vector driver: {proc.error}")
        return None

    def _report_sanitizer_findings(self, findings: list[SanitizerFinding], mismatches: list[str]) -> None:
        driver = self.vectors.driver
        for finding in findings:
            diagnostic = finding.diagnostic
            diagnostic.message = f"{diagnostic.message}{SANITIZER_FINDING_SUFFIX}"
            self.diagnostics.append(diagnostic)
            self.failures.append(
                FailureReport(
                    id=f"lean:vectors:{diagnostic.tool}:{diagnostic.rule_id}",
                    kind="runtime",
                    tool=diagnostic.tool,
                    property_id=diagnostic.rule_id,
                    description=diagnostic.message,
                    location=diagnostic.location,
                    call_stack=finding.call_stack(),
                    raw_ref=self.raw_ref,
                    inputs=vector_inputs(mismatches[0]) if mismatches else [],
                    explanation=f"{diagnostic.tool} reported {diagnostic.rule_id} while the trusted driver {driver} "
                    "ran the vectors generated from the Lean model; a sanitizer report is definite evidence of "
                    "undefined behaviour.",
                )
            )

    def _report_mismatches(self, mismatches: list[str]) -> None:
        for text in mismatches[:MAX_REPORTED_MISMATCHES]:
            pairs = " ".join(f"{name}={value}" for name, value in _KEY_VALUE_RE.findall(text))
            key = pairs or text[:MAX_MISMATCH_KEY_CHARS]
            self.failures.append(
                FailureReport(
                    id=f"lean:vectors:mismatch:{key}",
                    kind="test-oracle",
                    tool="lean-vectors",
                    property_id="vector-mismatch",
                    description=f"the C implementation disagrees with the Lean model: {text[:MAX_VECTOR_TEXT_CHARS]}",
                    inputs=vector_inputs(text),
                    raw_ref=self.raw_ref,
                    explanation=f"{self.vectors.driver} compared the implementation with the expected output computed "
                    f"by the Lean model ({self.lean.spec_file}) for this vector.",
                )
            )
            self.diagnostics.append(
                Diagnostic(
                    tool="lean-vectors",
                    rule_id="vector-mismatch",
                    severity="error",
                    message=f"vector mismatch: {text[:MAX_VECTOR_TEXT_CHARS]}",
                )
            )

    def _disagreement_summary(self, findings: list[SanitizerFinding], mismatches: list[str]) -> str:
        summary = f"{len(mismatches)} mismatching vector(s)" + (
            f", first: {mismatches[0][:MAX_FIRST_MISMATCH_CHARS]}" if mismatches else ""
        )
        if findings:
            summary += f"; {findings[0].diagnostic.tool} {findings[0].diagnostic.rule_id}"
        return summary

    def _judge_driver_run(self) -> dict:
        proc = self.driver_run
        spec = self.spec
        sig = untrusted_program_terminating_signal(proc)
        project_files = {*spec.sources.editable, *spec.sources.headers, self.vectors.driver}
        findings = parse_sanitizer_findings(proc.stderr, project_files=project_files, base_dir=str(self.snapshot))
        mismatches = [match.group(1).strip() for match in _MISMATCH_RE.finditer(proc.stdout + "\n" + proc.stderr)]
        self._report_sanitizer_findings(findings, mismatches)
        self._report_mismatches(mismatches)
        self.details["mismatches"] = len(mismatches)
        if findings or mismatches:
            return self._done("fail", self._disagreement_summary(findings, mismatches))
        if proc.output_truncated:
            return self._output_limit_exceeded()
        if proc.timed_out or sig == _signal.SIGXCPU:
            return self._done("timeout", f"the vector driver did not finish within {self.run_budget:.0f}s")
        if proc.exit_code != 0:
            return self._driver_failed(proc, sig)
        return self._judge_vector_count(proc)

    def _output_limit_exceeded(self) -> dict:
        self.failures.append(
            FailureReport(
                id="lean:vectors:output-limit",
                kind="test-oracle",
                tool="lean-vectors",
                property_id="output-limit-exceeded",
                raw_ref=self.raw_ref,
                description="the vector driver exceeded tests.max_output_bytes",
            )
        )
        return self._done("fail", "the vector driver exceeded the output limit")

    def _driver_failed(self, proc: ProcResult, sig: int | None) -> dict:
        got = f"terminated by signal {sig}" if sig is not None else f"exit code {proc.exit_code}"
        self.failures.append(
            FailureReport(
                id="lean:vectors:exit-code",
                kind="test-oracle",
                tool="lean-vectors",
                property_id="exit-code",
                description=f"the vector driver failed ({got}) without reporting a mismatching vector",
                raw_ref=self.raw_ref,
                explanation="See the raw output of the vector run.",
            )
        )
        return self._done("fail", f"the vector driver failed ({got})")

    def _judge_vector_count(self, proc: ProcResult) -> dict:
        checked = _CHECKED_RE.findall(proc.stdout)
        if len(checked) != 1 or not _is_vector_count(checked[0]):
            return self._done(
                "error",
                f"the vector driver must print exactly one '{VECTOR_CHECKED_TAG} <n>' line "
                f"(found {len(checked)}); the vector run is not conclusive",
            )
        count = int(checked[0])
        if count != self.vector_count:
            return self._done(
                "error",
                f"the vector driver checked {count} of {self.vector_count} vector(s); the vector run is not conclusive",
            )
        return self._done("pass", f"{count}/{self.vector_count} test vector(s) agree with the model")
