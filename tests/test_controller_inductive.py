from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from spec_inductive_support import inductive_section, spec_data, spec_with

from leanc.backends.inductive import run_inductive
from leanc.foundation.models import ApprovalCheck, StageResult
from leanc.foundation.status import Outcome, Status
from leanc.pipeline import controller
from leanc.pipeline.controller import (
    FORMAL_GATES,
    GATES_ANALYSING_OR_EXECUTING_CANDIDATE_CODE,
    INDUCTIVE_LIMITATION,
    INTEGRITY_FAILED,
    INTEGRITY_NOT_RUN,
    PROTECTED_FILE_CHANGED,
    _assumptions,
    _GATE_RUNNERS,
    _GateSequence,
    _limitations,
)
from leanc.specs.loader import load_spec
from leanc.specs.model import INDUCTIVE_GATE
from leanc.toolchain.context import VerifyOptions

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
PLAIN_EXAMPLE = "safe_array_get"
SPEC_HASH = "ab" * 32
INDUCTIVE_ASSUMPTION = "every execution of main() of harness/inductive.c from every state its LEANC_ASSUMEs admit"
RECHECK_BEFORE_INDUCTIVE = f"before the {INDUCTIVE_GATE} gate"
TAMPERED_REASON = "protected file harness/inductive.c changed"


class _Recorder:
    def __init__(self, tampered: bool = False) -> None:
        self.events: list[str] = []
        self.tampered = tampered

    def run(self, ctx) -> StageResult:
        self.events.append("run")
        return StageResult(stage=INDUCTIVE_GATE, outcome=Outcome.PASS, summary="stub inductive pass")

    def recheck(self, spec, ws, ctx, when: str) -> StageResult | None:
        self.events.append(when)
        if not self.tampered:
            return None
        return StageResult(
            stage="spec",
            outcome=Outcome.FAIL,
            status=Status.SPEC_UNAPPROVED,
            summary=TAMPERED_REASON,
            details={"reasons": [f"{when}: {TAMPERED_REASON}"]},
        )

    def install(self, monkeypatch) -> _Recorder:
        monkeypatch.setitem(controller._GATE_RUNNERS, INDUCTIVE_GATE, self.run)
        monkeypatch.setattr(controller, "_recheck_protected", self.recheck)
        return self


def _sequence(integrity_blocked_reason: str | None) -> _GateSequence:
    return _GateSequence(
        spec=load_spec(EXAMPLES / PLAIN_EXAMPLE / "spec.yaml"),
        ws=None,
        ctx=None,
        options=VerifyOptions(),
        approval=ApprovalCheck(approved=True, spec_hash=SPEC_HASH),
        stages=[],
        integrity_blocked_reason=integrity_blocked_reason,
    )


def test_inductive_is_a_formal_gate_on_candidate_code():
    assert INDUCTIVE_GATE in FORMAL_GATES
    assert INDUCTIVE_GATE in GATES_ANALYSING_OR_EXECUTING_CANDIDATE_CODE


def test_inductive_gate_is_skipped_when_integrity_failed(monkeypatch):
    recorder = _Recorder().install(monkeypatch)
    sequence = _sequence(INTEGRITY_FAILED)
    sequence._run_or_skip(INDUCTIVE_GATE)
    assert recorder.events == []
    stage = sequence.stages[-1]
    assert stage.stage == INDUCTIVE_GATE and stage.outcome is Outcome.SKIPPED
    assert INTEGRITY_FAILED in stage.summary and stage.status is Status.UNKNOWN


def test_inductive_gate_is_skipped_when_integrity_did_not_run(monkeypatch):
    recorder = _Recorder().install(monkeypatch)
    sequence = _sequence(INTEGRITY_NOT_RUN)
    sequence._run_or_skip(INDUCTIVE_GATE)
    assert recorder.events == []
    assert sequence.stages[-1].outcome is Outcome.SKIPPED


def test_approval_is_rechecked_before_the_inductive_gate_runs(monkeypatch):
    recorder = _Recorder().install(monkeypatch)
    sequence = _sequence(None)
    sequence._run_or_skip(INDUCTIVE_GATE)
    assert recorder.events == [RECHECK_BEFORE_INDUCTIVE, "run"]
    assert sequence.stages[-1].outcome is Outcome.PASS


def test_protected_change_before_the_inductive_gate_skips_it_and_revokes_approval(monkeypatch):
    recorder = _Recorder(tampered=True).install(monkeypatch)
    sequence = _sequence(None)
    sequence._run_or_skip(INDUCTIVE_GATE)
    assert recorder.events == [RECHECK_BEFORE_INDUCTIVE]
    assert [s.stage for s in sequence.stages] == ["spec", INDUCTIVE_GATE]
    assert sequence.stages[-1].outcome is Outcome.SKIPPED
    assert PROTECTED_FILE_CHANGED in sequence.stages[-1].summary
    assert not sequence.approval.approved


def test_inductive_assumptions_reach_the_verification_result():
    spec = load_spec(EXAMPLES / PLAIN_EXAMPLE / "spec.yaml")
    stage = StageResult(stage=INDUCTIVE_GATE, outcome=Outcome.PASS, details={"assumptions": [INDUCTIVE_ASSUMPTION]})
    assumptions = _assumptions(spec, [stage], ApprovalCheck(approved=True, spec_hash=SPEC_HASH))
    assert INDUCTIVE_ASSUMPTION in assumptions


SANDBOXED = SimpleNamespace(sandbox=SimpleNamespace(active=True, note=""))


def _spec_with_gates(gates: list[str]):
    data = spec_data()
    data["gates"] = gates
    return spec_with(inductive_section(), data)


def test_inductive_gate_is_registered_with_its_runner():
    assert _GATE_RUNNERS[INDUCTIVE_GATE] is run_inductive


def test_inductive_limitation_is_listed_only_when_the_spec_requires_the_gate():
    approval = ApprovalCheck(approved=True, spec_hash=SPEC_HASH)
    with_gate = _spec_with_gates(["compile", "cbmc", INDUCTIVE_GATE])
    section_only = _spec_with_gates(["compile", "cbmc"])
    assert INDUCTIVE_LIMITATION in _limitations(with_gate, SANDBOXED, approval, [])
    assert INDUCTIVE_LIMITATION not in _limitations(section_only, SANDBOXED, approval, [])
