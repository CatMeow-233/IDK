from __future__ import annotations

from leanc.backends.wp.argv import RTE_CLASSES
from leanc.backends.wp.compile_flags import FRAMA_C_STANDARD, classify_wp_compile_flags
from leanc.foundation.status import Outcome
from leanc.specs.model import Spec
from leanc.toolchain.context import BackendContext

WHY3_VERSION_PREFIX = "Why3 platform, version "
NOT_CHECKED_BY_RTE = "reads of uninitialised memory, pointer alignment (\\aligned), function-pointer validity"
MACHINE_MODEL_FACTS = "32-bit int, 64-bit long and pointers, signed char, little-endian"
UNAPPLIED_FLAGS_CONSEQUENCE = (
    "Frama-C preprocesses with its own machdep, C11, the include directories and compile.defines only; "
    "behaviour that depends on these flags is not covered by the proof"
)


def _no_guarantee(target: str, outcome: Outcome) -> list[str]:
    return [
        f"Frama-C/WP did not establish the approved contract of {target}() (outcome {outcome.value}): "
        "no deductive guarantee is claimed"
    ]


def _model_only_proof(spec: Spec, outcome: Outcome) -> list[str]:
    unapplied = classify_wp_compile_flags(spec.compile.flags).not_applied
    return [
        f"Frama-C/WP proved the approved contract of {spec.target.function}() only under Frama-C's own model "
        f"(outcome {outcome.value}): compile.flags not applied by Frama-C ({', '.join(unapplied)}) change the "
        "semantics of the built program, so no deductive guarantee is claimed for it"
    ]


def _termination_scope(require_termination: bool) -> str:
    if require_termination:
        return "total correctness: termination was required and proved (terminates / loop variants / decreases)"
    return (
        "partial correctness only: termination is NOT proved (wp.require_termination is false); a "
        "non-terminating execution satisfies the contract vacuously"
    )


def _library_scope(generated_clauses: list[str]) -> str:
    generated = (
        " (default exits/terminates clauses generated for: " + ", ".join(generated_clauses) + ")"
        if generated_clauses
        else ""
    )
    assumed = "library calls are justified by Frama-C's libc ACSL specifications, which are assumed, not proved"
    return assumed + generated


def compile_statements(spec: Spec) -> list[str]:
    statements = []
    if spec.language.standard != FRAMA_C_STANDARD:
        statements.append(
            f"C language standard {spec.language.standard}: Frama-C preprocesses and parses the code as "
            f"{FRAMA_C_STANDARD}"
        )
    unapplied = classify_wp_compile_flags(spec.compile.flags).not_applied
    if unapplied:
        statements.append(
            "compile.flags not applied by Frama-C: " + ", ".join(unapplied) + f" - {UNAPPLIED_FLAGS_CONSEQUENCE}"
        )
    return statements


def wp_assumptions(ctx: BackendContext, details: dict, outcome: Outcome, model_outcome: Outcome) -> list[str]:
    spec = ctx.spec
    wp = spec.wp
    target = spec.target.function
    if outcome is not Outcome.PASS and model_outcome is Outcome.PASS:
        return _model_only_proof(spec, outcome) + compile_statements(spec)
    if outcome is not Outcome.PASS:
        return _no_guarantee(target, outcome)
    contracts = ", ".join(f"{c['file']}:{c['line']}" for c in details.get("contract", [])) or "?"
    provers = ", ".join(f"{p} {details['prover_versions'].get(p) or ''}".strip() for p in wp.provers)
    why3_version = (details.get("why3_version") or "?").replace(WHY3_VERSION_PREFIX, "")
    statements = [
        f"deductive proof with Frama-C {details.get('frama_c_version') or '?'} / WP (Why3 "
        f"{why3_version}; provers: {provers}; "
        f"{details.get('prover_timeout_s')} s per goal) of the approved ACSL contract of {target}() ({contracts}) "
        "for ALL inputs satisfying its preconditions - unbounded, no input-size or loop bound",
        "absence of runtime errors for all such inputs, as encoded by Frama-C's RTE guards (-wp-rte): "
        + ", ".join(details.get("rte_checks") or RTE_CLASSES)
        + f"; NOT checked: {NOT_CHECKED_BY_RTE}",
        _termination_scope(wp.require_termination),
        "under WP's Typed memory model (memory is accessed through its declared types; casts between "
        "incompatible pointer types are not modelled) and Frama-C's machine model "
        f"{details.get('machdep')} ({MACHINE_MODEL_FACTS})",
        _library_scope(details.get("library_functions_with_generated_clauses") or []),
        "loop invariants, loop variants, assertions and helper-function contracts written in the editable sources "
        "are proof obligations (all proved), not assumptions",
        f"non-vacuity (best effort): all {(details.get('smoke_tests') or {}).get('total', 0)} WP smoke tests passed - "
        "the provers found no inconsistent precondition, behaviour assumption, dead code or dead loop",
        "the result concerns the C source as preprocessed and parsed by Frama-C; compiler correctness and the "
        "platform's actual libc are outside its scope",
    ]
    statements += compile_statements(spec)
    statements += [f"WP memory-model hypothesis (assumed): {h}" for h in details.get("memory_model_hypotheses") or []]
    return statements
