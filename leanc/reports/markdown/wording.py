from __future__ import annotations

import re

from leanc.foundation.inductive_scope import COVERED_EXECUTIONS, HARNESS_NARROWING, SUBOBJECT_GAP, UNREACHED_STATES
from leanc.foundation.status import Status

STATUS_MEANING: dict[Status, str] = {
    Status.VERIFIED_BOUNDED: (
        "CBMC found no violation of the checked property classes for the approved harness, within its "
        "input constraints and the approved unwind bound; every loop was fully unwound (the unwinding "
        "assertions held), the harness was shown to be non-vacuous, and every other required gate passed. "
        "This is a BOUNDED result: it holds only within that harness, those input constraints, that unwind "
        "bound and those checked property classes. It says nothing about other inputs, larger sizes or "
        "iteration counts, or properties that were not checked, and it is NOT a proof of correctness "
        "(it is never equal to PROVED)."
    ),
    Status.VERIFIED_INDUCTIVE: (
        "Everything VERIFIED_BOUNDED requires holds (CBMC found no violation within the approved bounds and every "
        f"other required gate passed), and the opt-in `inductive` gate additionally showed that {COVERED_EXECUTIONS} "
        "and the mandatory check classes only (bounds, pointer, div-by-zero, signed-overflow, pointer-overflow, "
        "undefined-shift), with every contracted loop of the contracts file proved by induction: its invariant holds "
        "at entry and is preserved, its writes stay within its assigns clause and its decreases clause shows "
        "termination. Non-const static objects start arbitrary and allocations are assumed to succeed. Note: "
        f"{HARNESS_NARROWING}. Not proved: functions that are not claimed, {UNREACHED_STATES} and the extra "
        f"verification.checks classes; for those only the bounded CBMC result holds. Known gaps: {SUBOBJECT_GAP}; "
        "calls of CBMC library models such as strlen, strcpy, strncpy and strncat make "
        "the gate fail closed (it cannot pass: an incompleteness, not a false pass). The result is only as strong "
        "as the whole harness (its assumptions, its other code and its assertions), and it is NOT a proof against "
        "a contract (it is never equal to PROVED)."
    ),
    Status.PROVED: (
        "Frama-C/WP (the `wp` gate) discharged every proof obligation of the APPROVED ACSL contract and every "
        "runtime-error guard (-wp-rte), its smoke tests found no inconsistent precondition or dead code, and every "
        "other required gate passed. This is a deductive result for ALL inputs satisfying the contract's "
        "preconditions - not bounded by an unwind limit - but it holds only for that contract (a weak contract "
        "proves little), under WP's memory and machine model, the Frama-C libc specifications and the listed "
        "provers, and it is partial correctness unless the spec required termination. CBMC never produces "
        "PROVED, and Lean model proofs never upgrade a C verdict to PROVED."
    ),
    Status.COMPILE_ERROR: (
        "The sources did not compile (or could not be linked with the trusted test driver, or a required "
        "source file is missing) with the approved compiler, flags and warnings policy. Later gates could "
        "not run on this code, so nothing about its behaviour was established."
    ),
    Status.STATIC_ANALYSIS_WARNING: (
        "The Clang Static Analyzer reported at least one finding and the approved policy is `block`. "
        "Analyzer findings are path-sensitive heuristics (false positives and false negatives are possible), "
        "but under the approved policy they fail verification. Nothing was verified as correct."
    ),
    Status.RUNTIME_TEST_FAILED: (
        "At least one trusted test case failed when executed under the sanitizers (ASan/UBSan/LSan): a "
        "sanitizer reported an error, the program crashed or timed out, or the test oracle saw an exit "
        "code / output different from the expected one. This is concrete evidence of a defect on an "
        "executed path."
    ),
    Status.COUNTEREXAMPLE_FOUND: (
        "CBMC found an execution of the approved harness, within its input constraints and the unwind bound, "
        "that violates a checked property. The counterexample (inputs and trace) is concrete evidence of a "
        "defect relative to the approved specification, under CBMC's model of C; it remains valid even if "
        "the unwind bound was insufficient."
    ),
    Status.UNKNOWN: (
        "No verdict could be established. Typical causes: insufficient unwinding (an unwinding assertion "
        "failed), a vacuous harness (a reachability probe was unreachable, or vacuity could not be checked), "
        "a skipped required gate (excluded by --gates, no sandbox for dynamic tests, an earlier failure), or "
        "no formal gate in the spec. The absence of reported defects here is NOT evidence of correctness."
    ),
    Status.TIMEOUT: (
        "A tool exceeded its time limit before finishing (e.g. the CBMC solver or a test run). Nothing can be "
        "concluded from the unfinished gate; it is neither evidence of correctness nor of a defect."
    ),
    Status.TOOL_ERROR: (
        "A tool was missing, crashed, ran out of resources or produced output that could not be parsed (or "
        "an internal Lean C error occurred). The affected gate produced no verdict; this is neither evidence "
        "of correctness nor of a defect in the code."
    ),
    Status.SPEC_UNAPPROVED: (
        "There is no trusted specification: the spec is not approved, its approval signature is invalid, or "
        "the spec or a protected file (harness, test driver, headers) changed since approval. Nothing was "
        "formally verified against a trusted spec; any gate output shown is informational only."
    ),
    Status.REPAIR_EXHAUSTED: (
        "The repair workflow ended (round limit reached, proposals rejected by policy, no improvement, or a "
        "cycle) without reaching a successful verification. The workspace was not updated with a verified "
        "candidate; the remaining failures are those of the last accepted verification."
    ),
}

MOCK_DISCLAIMER = "mock provider: fixed fixtures, not LLM performance"

ALWAYS_LIMITATIONS: tuple[str, ...] = (
    "Sanitizers (ASan/UBSan/LSan) only cover executed paths: they can only report defects on the paths "
    "executed by the trusted test cases; untested inputs and paths may still be defective.",
    "CBMC results are bounded: they hold only for the approved harness, its input constraints, the unwind "
    "bound and the checked property classes, under CBMC's C semantics and memory model; a bounded result "
    "is never a proof (PROVED).",
)

_RESTATEMENTS_OF_ALWAYS_LIMITATIONS = (
    re.compile(r"^sanitizer[^.;:]*\bonly cover", re.I),
    re.compile(r"^cbmc results are bounded\b", re.I),
)

TOOL_LABELS: dict[str, str] = {
    "clang": "compiler diagnostics",
    "clang-static-analyzer": "Clang Static Analyzer",
    "asan": "AddressSanitizer",
    "ubsan": "UndefinedBehaviorSanitizer",
    "lsan": "LeakSanitizer",
    "test-oracle": "expected exit code / output of the trusted tests",
    "cbmc": "CBMC bounded model checker",
    "leanc": "Lean C itself",
    "leanc-policy": "Lean C repair policy",
}


def restates_always_limitation(item: str) -> bool:
    return any(pattern.search(item.strip()) for pattern in _RESTATEMENTS_OF_ALWAYS_LIMITATIONS)
