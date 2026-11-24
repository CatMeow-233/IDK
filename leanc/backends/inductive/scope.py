from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from leanc.backends.cbmc.argv import compile_translation
from leanc.backends.cbmc.assumptions import UNAPPLIED_FLAGS_CONSEQUENCE
from leanc.backends.cbmc.compile_flags import platform_architecture
from leanc.backends.inductive.build import InductiveBuild
from leanc.backends.inductive.evidence import EvidencedLoop
from leanc.backends.inductive.runs import INDUCTIVE_CHECKS
from leanc.foundation.status import Outcome
from leanc.toolchain.context import BackendContext

MEMORY_MODELS: tuple[str, ...] = ("memcpy", "memmove", "memset")
LIBRARY_LOOP_MODELS: tuple[str, ...] = ("strlen", "strcpy", "strncpy", "strncat")
HARNESS_ASSERTIONS_CLASS = "every assertion of the inductive harness"
LOOP_CONTRACT_CLASSES = "the loop invariant, assigns and decreases clauses of every contracted loop"
COPY_GAP = (
    "known gap (not detected): a memcpy, memmove or memset that overflows into a neighbouring field of the same "
    "object, or that copies into a string literal, is not reported (the copy models check whole objects only)"
)
LIBRARY_GAP = (
    "known gap (fail closed): calls of CBMC library models that contain loops ("
    + ", ".join(LIBRARY_LOOP_MODELS)
    + ") fail their loop-contract write-set checks, so such code never passes this gate (INCONCLUSIVE, never a pass)"
)
NOT_PROVED = "VERIFIED_INDUCTIVE is not PROVED: PROVED is reached only through a passing required wp gate"
NO_CLAIM = (
    "the inductive gate ended {outcome}: it establishes no inductive claim, and VERIFIED_INDUCTIVE needs a "
    "passing required inductive gate"
)


@dataclass(frozen=True)
class ScopeInputs:
    unwind: int
    version: str | None
    platform: str | None
    loops: Mapping[str, EvidencedLoop]


def _sha256(ctx: BackendContext, rel: str) -> str | None:
    path = ctx.snapshot_dir / rel
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _loop_text(location: str, loop: EvidencedLoop | None) -> str:
    if loop is None or not loop.function:
        return location
    return f"{location} in {loop.function}()"


def _loops_statement(build: InductiveBuild, loops: Mapping[str, EvidencedLoop]) -> str:
    if not build.contracts:
        return "contracted loops: none (the claimed calls reach no loop outside the harness and the CBMC library)"
    shown = ", ".join(_loop_text(item.contract.location, loops.get(item.contract.location)) for item in build.contracts)
    return f"contracted loops proved by induction (invariant at entry and after one step, assigns, decreases): {shown}"


def _assumptions_statement(build: InductiveBuild, harness: str) -> str:
    if not build.harness_assumptions:
        return f"harness assumptions of {harness}: none (every state of the harness inputs)"
    return f"harness assumptions of {harness} (executions outside them are not covered): " + "; ".join(
        build.harness_assumptions
    )


def _checks_statement(ctx: BackendContext) -> str:
    extra = [check for check in ctx.spec.effective_cbmc_checks() if check not in INDUCTIVE_CHECKS]
    statement = (
        f"checked property classes: the mandatory CBMC check classes only ({', '.join(INDUCTIVE_CHECKS)}), plus "
        f"{HARNESS_ASSERTIONS_CLASS} and {LOOP_CONTRACT_CLASSES}"
    )
    if extra:
        statement += f"; not checked by this gate (bounded CBMC result only): {', '.join(extra)}"
    return statement


def _translation_statements(ctx: BackendContext, platform: str | None) -> list[str]:
    statements = [f"C semantics as modelled by CBMC for {platform}"] if platform else []
    unapplied = compile_translation(ctx.spec).not_applied_on(platform_architecture(platform))
    if unapplied:
        statements.append(
            "compile.flags not applied by CBMC (as in the cbmc stage): " + ", ".join(unapplied) + f" - {UNAPPLIED_FLAGS_CONSEQUENCE}"
        )
    return statements


def scope_statements(ctx: BackendContext, build: InductiveBuild, inputs: ScopeInputs) -> list[str]:
    inductive = ctx.spec.inductive
    harness = inductive.harness
    functions = ", ".join(f"{name}()" for name in inductive.functions)
    statements = [
        (
            f"inductive proof with CBMC {inputs.version or '(unknown version)'} and DFCC loop contracts: every "
            f"execution of main() of the protected inductive harness {harness} (sha256 {_sha256(ctx, harness)}) "
            "from every state its harness assumptions admit, with at most one call of a claimed function per path"
        ),
        f"claimed functions: {functions}; functions that are not claimed have only the bounded CBMC result",
        _loops_statement(build, inputs.loops),
        _assumptions_statement(build, harness),
        _checks_statement(ctx),
        (
            "memory model: " + ", ".join(MEMORY_MODELS) + " are replaced by models that check the source and "
            "destination ranges and make the destination contents arbitrary; static objects start arbitrary; "
            "allocations never fail"
        ),
        (
            f"loops of the harness and of the CBMC library and recursion unwound at most {inputs.unwind} time(s); "
            "unwinding assertions are enabled"
        ),
        COPY_GAP,
        LIBRARY_GAP,
        NOT_PROVED,
    ]
    return statements + _translation_statements(ctx, inputs.platform)


def no_claim_statements(outcome: Outcome) -> list[str]:
    return [NO_CLAIM.format(outcome=outcome.value)]


def _loop_details(build: InductiveBuild, loops: Mapping[str, EvidencedLoop]) -> list[dict[str, Any]]:
    details = []
    for item in build.contracts:
        contract, loop = item.contract, loops.get(item.contract.location)
        details.append(
            {
                "file": contract.file,
                "line": contract.line,
                "function": loop.function if loop else None,
                "cbmc_loop": loop.name if loop else None,
                "invariant": [entry.render() for entry in contract.invariant],
                "assigns": [target.render() for target in contract.assigns],
                "decreases": contract.decreases.render(),
            }
        )
    return details


def scope_details(
    ctx: BackendContext, build: InductiveBuild, inputs: ScopeInputs, property_classes: Mapping[str, int]
) -> dict[str, Any]:
    inductive = ctx.spec.inductive
    verification = ctx.spec.verification
    return {
        "harness": {"path": inductive.harness, "sha256": _sha256(ctx, inductive.harness)},
        "contracts": {"path": inductive.contracts, "sha256": _sha256(ctx, inductive.contracts)},
        "functions": list(inductive.functions),
        "loops": _loop_details(build, inputs.loops),
        "property_classes": dict(property_classes),
        "check_classes": list(INDUCTIVE_CHECKS),
        "harness_assumptions": list(build.harness_assumptions),
        "memory_models": list(MEMORY_MODELS),
        "known_gaps": [COPY_GAP, LIBRARY_GAP],
        "unwind": inputs.unwind,
        "object_bits": verification.object_bits if verification else None,
        "cbmc_version": inputs.version,
    }


def contracted_locations(build: InductiveBuild) -> Sequence[str]:
    return [item.contract.location for item in build.contracts]
