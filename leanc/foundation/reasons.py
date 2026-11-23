from __future__ import annotations

from enum import Enum


class Reason(str, Enum):
    UNWIND_INSUFFICIENT = "unwind_insufficient"
    VACUOUS_HARNESS = "vacuous_harness"
    VACUITY_UNCHECKED = "vacuity_unchecked"
    UNMODELLED_CALLEE = "unmodelled_callee"
    UNDECLARED_FUNCTION = "undeclared_function"
    FRAMA_C_MODEL_ONLY_PROOF = "frama_c_model_only_proof"
    SUSPECTED_NON_TERMINATION = "suspected_non_termination"
    INDUCTIVE_STEP_FAILED = "inductive_step_failed"
    PROPERTY_UNPROVED = "property_unproved"
    CONTRACT_REJECTED = "contract_rejected"
    LOOP_WITHOUT_CONTRACT = "loop_without_contract"
    GATE_FAILED = "gate_failed"
    GATE_SKIPPED = "gate_skipped"
    GATE_INCONCLUSIVE = "gate_inconclusive"
    GATE_TIMEOUT = "gate_timeout"
    TOOL_ERROR = "tool_error"
    NO_FORMAL_GATE = "no_formal_gate"

    def __str__(self) -> str:
        return self.value


STAGE_REASON_CODES_DETAIL = "reason_codes"
