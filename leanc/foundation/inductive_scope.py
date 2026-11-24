from __future__ import annotations

__all__ = [
    "COVERED_EXECUTIONS",
    "HARNESS_NARROWING",
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
