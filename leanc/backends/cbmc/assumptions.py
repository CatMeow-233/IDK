from __future__ import annotations

from leanc.specs.model import InputBound
from leanc.toolchain.context import BackendContext


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


def scope_assumptions(
    ctx: BackendContext,
    *,
    version: str | None,
    platform: str | None = None,
    vacuity: dict | None = None,
    no_body: list[str] | None = None,
    dropped_defines: list[str] | None = None,
) -> list[str]:
    verification = ctx.spec.verification
    statements = [
        _unwinding_statement(ctx, version),
        "checked property classes: "
        + ", ".join(verification.effective_checks())
        + " - plus every assertion of the approved harness; other properties are not checked",
        f"inputs as constrained by the approved harness {verification.harness} (its LEANC_ASSUME preconditions) "
        f"calling {ctx.spec.target.function}(); executions outside these constraints are not covered",
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
    if no_body:
        statements.append(
            "functions without a body are treated by CBMC as returning arbitrary values: " + ", ".join(sorted(no_body))
        )
    if dropped_defines:
        statements.append(
            "compile define(s) not passed to CBMC because they would disable harness assertions: "
            + ", ".join(dropped_defines)
        )
    return statements
