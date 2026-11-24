from __future__ import annotations

__all__ = [
    "COVERED_EXECUTIONS",
    "HARNESS_NARROWING",
    "SUBOBJECT_GAP",
    "SUBOBJECT_GAP_SHORT",
    "UNREACHED_STATES",
]

COVERED_EXECUTIONS = (
    "every execution of main() of the protected inductive harness as written, with at most one call of a claimed "
    "function (spec inductive.functions) per path, satisfies the harness assertions that execute on it"
)
HARNESS_NARROWING = (
    "harness code other than LEANC_ASSUME also narrows the covered states and is not listed (declaration "
    "initialisers; writes before the last LEANC_ASSUME; writes after the first claimed call of main(), before a "
    "later claimed call included; any write in main() when it calls no claimed function directly; branches around "
    "the assertions; helper functions): the listed LEANC_ASSUMEs are an upper bound of the covered states, not the "
    "covered set, so the approver must review the whole harness"
)
UNREACHED_STATES = "states the harness as written does not reach"
SUBOBJECT_GAP = (
    "an access through a pointer that overflows a member array into a neighbouring field or the padding of the "
    "same object (indexing such as p->a[i], pointer arithmetic, a pointer taken from the member such as q = p->a; "
    "q[i], and the memcpy, memmove and memset models), or a copy into a string literal, is not reported: CBMC "
    "checks pointer-based accesses against the whole object only (accesses on a named object such as s.a[i] are "
    "checked against the member)"
)
SUBOBJECT_GAP_SHORT = (
    "pointer accesses and copies that overflow a member array inside its object, or copies into a string literal, "
    "are not reported"
)
