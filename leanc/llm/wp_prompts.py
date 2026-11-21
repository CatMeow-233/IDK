"""Prompt additions for specs that use the Frama-C/WP gate (V2).

Kept separate from :mod:`leanc.llm.prompts` (a shared module) so the WP
guidance can evolve with :mod:`leanc.backends.wp` and
:mod:`leanc.rules.acsl`, which define what is actually accepted.
"""

from __future__ import annotations

from leanc.specs.model import Spec

WP_REPAIR_NOTE = """\
## Frama-C/WP proof obligations (this spec uses the `wp` gate)
The approved ACSL contract of `{target}` is in the protected header(s) {headers}; WP proves the editable code \
against it for ALL inputs, with runtime-error guards (-wp-rte) and smoke tests.{termination}
- An UNPROVED goal (FailureReport kind "unproved") is not a counterexample: if the code is right, add PROOF \
ANNOTATIONS in the editable sources; if it is wrong, fix the code.
- Allowed ACSL in editable code: inside function bodies `/*@ loop invariant ...; loop assigns ...; loop variant \
...; */` (directly before the loop), `/*@ assert ...; */`, `/*@ check ...; */`; contracts on NON-target `static` \
helper functions, written directly before the helper's definition; non-recursive `predicate` / `logic` \
definitions and `lemma`s. All of these are proved by WP, never assumed.
- Forbidden (the candidate is rejected): any ACSL contract on `{target}` itself (Frama-C would merge it with the \
approved one), `admit`, `axiom` / `axiomatic`, `inductive`, recursive logic definitions, `ghost` code, statement \
contracts, `\\false` in `requires`, macros inside annotations, `#define` of identifiers used by the approved \
contract.
- Loop invariants must cover every variable the loop modifies (state its bounds and the property accumulated so \
far) and be followed by `loop assigns` listing exactly those variables; at loop exit the invariants plus the \
negated loop condition must imply the post-condition.
"""


def wp_repair_note(spec: Spec) -> str | None:
    """Extra repair-prompt section for specs with the ``wp`` gate (else None)."""
    if "wp" not in spec.gates or spec.wp is None:
        return None
    termination = (
        " Termination is required: every loop needs a `loop variant` (and recursion a `decreases`)."
        if spec.wp.require_termination
        else ""
    )
    return WP_REPAIR_NOTE.format(
        target=spec.target.function,
        headers=", ".join(f"`{h}`" for h in spec.sources.headers) or "(none)",
        termination=termination,
    )
