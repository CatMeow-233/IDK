from __future__ import annotations

from collections.abc import Iterable

from leanc.foundation.models import ReasonCode
from leanc.foundation.reasons import Reason
from leanc.specs.model import INDUCTIVE_GATE, MANDATORY_CHECKS, Spec

MAX_FEEDBACK_CODES = 20
NO_FEEDBACK = "(none)"
UNLOCATED = ""
FEEDBACK_PRIORITY: tuple[Reason, ...] = (
    Reason.CONTRACT_REJECTED,
    Reason.LOOP_WITHOUT_CONTRACT,
    Reason.INDUCTIVE_STEP_FAILED,
    Reason.SUSPECTED_NON_TERMINATION,
    Reason.VACUOUS_HARNESS,
    Reason.UNWIND_INSUFFICIENT,
    Reason.UNMODELLED_CALLEE,
    Reason.UNDECLARED_FUNCTION,
)

INDUCTIVE_REPAIR_NOTE = """\
## Inductive loop contracts (this spec uses the `inductive` gate)
The inductive gate checks each call of the claimed functions ({functions}) made by the protected inductive \
harness `{harness}`, from every state its harness assumptions admit, for its assertions and the mandatory check \
classes ({checks}). Every loop those calls reach needs a loop contract, which CBMC proves by induction: the \
invariant holds at loop entry and after one iteration from an arbitrary state satisfying it, the loop writes only \
what assigns names, and decreases strictly decreases. The contracts live ONLY in the editable JSON file \
`{contracts}` (it may not exist yet); answer with its complete new text under "files":
{{"loops": [{{"file": "<editable C file>", "line": <line of the for/while keyword>, "invariant": ["<expression>"], \
"assigns": ["<target>"], "decreases": "<expression>"}}]}}
- Every entry needs all five keys; file must be one of sources.editable; one entry per (file, line). Contract \
exactly the loops the gate reports as `loop_without_contract` (it gives file:line); only for and while loops whose \
body is in braces can carry a contract.
- Clauses are side-effect-free C expressions over the names in scope at the loop: no calls, assignment, ++/--, \
?:, comma, address-of, sizeof, casts other than to integer types, quantifiers or macro names (write the number), \
and no identifier that starts with `__` or `_` plus a capital letter, `nondet_`, `leanc_` or `LEANC_`. The only \
built-ins are `__CPROVER_loop_entry(<lvalue>)` inside invariant entries and `__CPROVER_object_whole(<pointer>)` / \
`__CPROVER_object_upto(<pointer>, <bytes>)` inside assigns entries; nowhere else (never in the C sources).
- assigns names exactly what the loop writes, including what called functions write; for memory prefer \
`__CPROVER_object_upto(ptr, bytes)` over the whole object when an outer invariant depends on its other fields.
- decreases is mandatory: an expression that strictly decreases on every iteration and stays bounded below, for \
example `hi - lo`.
- A failure of kind "unproved" is not a counterexample: its state is arbitrary within your invariant. If that \
state cannot occur, strengthen the invariant. `vacuous_harness` means a probe was never reached, for example \
because an invariant makes a loop body unreachable.
- Your contracts are proposals only: the grammar re-checks every clause and CBMC must prove it; nothing you write \
is assumed. Never weaken the code to make a contract pass."""

CONTRACTS_ONLY_NOTE = """\
THIS ROUND: only proof gates are open and the inductive gate is one of them, so change ONLY `{contracts}`. \
A change to any other file, the C sources included, is rejected (rule CONTRACTS_ONLY_ROUND) and wastes the round. \
If the C code itself is wrong, say so in root_cause and leave it unchanged: a human must fix it."""

INDUCTIVE_FEEDBACK_HEADING = "## Inductive gate feedback (reason codes of the current best verification)"


def reason_code_text(code: ReasonCode) -> str:
    where = f" at {code.location.short()}" if code.location is not None else UNLOCATED
    detail = f": {code.detail}" if code.detail else ""
    return f"{code.code.value} ({code.stage}){where}{detail}"


def _priority(code: ReasonCode) -> int:
    return FEEDBACK_PRIORITY.index(code.code) if code.code in FEEDBACK_PRIORITY else len(FEEDBACK_PRIORITY)


def by_feedback_priority(codes: Iterable[ReasonCode]) -> list[ReasonCode]:
    return sorted(codes, key=_priority)


def uses_inductive_gate(spec: Spec) -> bool:
    return INDUCTIVE_GATE in spec.gates and spec.inductive is not None


def inductive_repair_note(spec: Spec, *, contracts_only: bool) -> str | None:
    if not uses_inductive_gate(spec):
        return None
    inductive = spec.inductive
    note = INDUCTIVE_REPAIR_NOTE.format(
        functions=", ".join(f"{name}()" for name in inductive.functions),
        harness=inductive.harness,
        checks=", ".join(MANDATORY_CHECKS),
        contracts=inductive.contracts,
    )
    if contracts_only:
        note += "\n" + CONTRACTS_ONLY_NOTE.format(contracts=inductive.contracts)
    return note


def inductive_feedback(spec: Spec, codes: Iterable[ReasonCode], max_item_chars: int) -> str | None:
    if not uses_inductive_gate(spec):
        return None
    shown = by_feedback_priority(code for code in codes if code.stage == INDUCTIVE_GATE)[:MAX_FEEDBACK_CODES]
    lines = [f"- {reason_code_text(code)[:max_item_chars]}" for code in shown] or [NO_FEEDBACK]
    return "\n".join([INDUCTIVE_FEEDBACK_HEADING, *lines])
