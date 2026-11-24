from __future__ import annotations

from leanc.backends.cbmc.bodiless import UNMODELLED_CONSEQUENCE
from leanc.backends.cbmc.compile_flags import CompileFlagTranslation
from leanc.specs.model import InputBound
from leanc.toolchain.context import BackendContext

UNAPPLIED_FLAGS_CONSEQUENCE = (
    "CBMC checks the program without them: strict IEEE-754 floating-point semantics (no fast-math, no FP "
    "contraction), no -m ISA/target features, no optimisation effects, default C semantics for every other "
    "option; behaviour that depends on these flags is not covered"
)


def _bound_text(bound: InputBound) -> str:
    low, high = bound.min, bound.max
    if low is not None and high is not None:
        constraint = f"{low} <= {bound.name} <= {high}"
    elif low is not None:
        constraint = f"{bound.name} >= {low}"
    elif high is not None:
        constraint = f"{bound.name} <= {high}"
    else:
        return f"{bound.name}: {bound.note}" if bound.note else f"{bound.name}: no explicit bound"
    return constraint + (f" ({bound.note})" if bound.note else "")


def _unwinding_statement(ctx: BackendContext, version: str | None) -> str:
    verification = ctx.spec.verification
    statement = (
        f"bounded model checking with CBMC {version or '(unknown version)'}: "
        f"every loop / recursion unwound at most {verification.unwind} time(s)"
    )
    if verification.unwindset:
        per_loop = ", ".join(f"{loop}={bound}" for loop, bound in sorted(verification.unwindset.items()))
        statement += f" (per-loop bounds: {per_loop})"
    return statement + (
        "; unwinding assertions are enabled, so a loop needing more iterations makes the result inconclusive"
    )


def _model_statements(ctx: BackendContext) -> list[str]:
    verification = ctx.spec.verification
    malloc = (
        "malloc model: allocations may fail and then return NULL (--malloc-may-fail --malloc-fail-null)"
        if verification.malloc_may_fail
        else "malloc model: allocations never fail (CBMC default); out-of-memory paths are not explored"
    )
    pointer = (
        f"pointer model: {verification.object_bits} object bits"
        if verification.object_bits
        else "pointer model: CBMC default object bits"
    )
    return [malloc, pointer]


def _standard_statement(compile_options: CompileFlagTranslation) -> str:
    mode = compile_options.standard_mode
    if mode is None:
        return f"C language standard {compile_options.standard}: no CBMC mode, checked in CBMC's default C11 mode"
    statement = f"C language standard {compile_options.standard}: checked in CBMC's {mode.flag} mode"
    return statement + (f" ({mode.approximation})" if mode.approximation else "")


def _compile_statements(compile_options: CompileFlagTranslation, unapplied_flags: list[str]) -> list[str]:
    statements = [_standard_statement(compile_options)]
    if unapplied_flags:
        statements.append(
            "compile.flags not applied by CBMC: " + ", ".join(unapplied_flags) + f" - {UNAPPLIED_FLAGS_CONSEQUENCE}"
        )
    if compile_options.dropped_defines:
        statements.append(
            "compile define(s) not passed to CBMC because they would disable harness assertions: "
            + ", ".join(compile_options.dropped_defines)
        )
    return statements


def _bodiless_statements(bodiless: dict[str, list[str]]) -> list[str]:
    statements = []
    if bodiless.get("modelled"):
        statements.append(
            "functions without a body modelled by CBMC as built-ins or nondeterministic inputs: "
            + ", ".join(sorted(bodiless["modelled"]))
        )
    if bodiless.get("unmodelled"):
        statements.append(
            "functions without a body NOT modelled by CBMC: "
            + ", ".join(sorted(bodiless["unmodelled"]))
            + f" - {UNMODELLED_CONSEQUENCE}, so no verified verdict is possible"
        )
    return statements


def scope_assumptions(
    ctx: BackendContext,
    *,
    version: str | None,
    platform: str | None = None,
    vacuity: dict | None = None,
    bodiless: dict[str, list[str]] | None = None,
    compile_options: CompileFlagTranslation | None = None,
    unapplied_flags: list[str] | None = None,
) -> list[str]:
    verification = ctx.spec.verification
    statements = [
        _unwinding_statement(ctx, version),
        "checked property classes: "
        + ", ".join(ctx.spec.effective_cbmc_checks())
        + " - plus every assertion of the approved harness; other properties are not checked",
        (
            f"inputs as constrained by the approved harness {verification.harness} (its code and its LEANC_ASSUME preconditions) "
            f"calling {ctx.spec.target.function}(); executions outside these constraints are not covered"
        ),
    ]
    statements += [
        "input bound (documented in the spec, enforced by the harness): " + _bound_text(bound)
        for bound in verification.input_bounds
    ]
    statements += _model_statements(ctx)
    if platform:
        statements.append(f"C semantics as modelled by CBMC for {platform}")
    if vacuity is not None and vacuity.get("status") == "ok":
        probe_count = len(vacuity.get("properties") or vacuity.get("probes") or {})
        statements.append(f"non-vacuity: all {probe_count} LEANC_REACH probe(s) of the harness are reachable")
    statements += _bodiless_statements(bodiless or {})
    if compile_options is not None:
        statements += _compile_statements(compile_options, list(unapplied_flags or []))
    return statements
