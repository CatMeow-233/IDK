"""The Lean C structured specification (YAML / JSON).

A spec has two very different parts:

* ``requirement`` - the original natural-language requirement.  It is kept for
  context only; no verifier consumes it.
* everything else - the *formal* part: target interface, contract, harness,
  checks, bounds, tests, gates and limits.  Once approved (see
  :mod:`leanc.specs.approval`) the formal part, together with the harness, test
  driver and interface headers, is hash-locked.

Unknown keys are rejected so that nothing can be smuggled into a spec without
changing its hash and without schema validation noticing.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from leanc.foundation.workspace import normalize_relpath

SPEC_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
C_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: CBMC property classes Lean C knows how to request.  Mapping to flags lives in
#: :mod:`leanc.backends.cbmc`.
CheckName = Literal[
    "bounds",
    "pointer",
    "div-by-zero",
    "signed-overflow",
    "unsigned-overflow",
    "pointer-overflow",
    "conversion",
    "undefined-shift",
    "float-overflow",
    "nan",
    "memory-leak",
    "pointer-primitive",
    "enum-range",
]

#: Checks that are always enabled, whatever the spec says.  A spec cannot switch
#: them off, and therefore neither can a repair attempt.
MANDATORY_CHECKS: tuple[str, ...] = (
    "bounds",
    "pointer",
    "div-by-zero",
    "signed-overflow",
    "pointer-overflow",
    "undefined-shift",
)

GateName = Literal["compile", "static_analysis", "sanitizer", "cbmc", "wp", "lean"]
#: Default gate list (V1).  ``wp`` (Frama-C/WP deductive proof) and ``lean``
#: (Lean 4 model proofs) are opt-in V2 gates.
ALL_GATES: tuple[str, ...] = ("compile", "static_analysis", "sanitizer", "cbmc")
GATE_ORDER: tuple[str, ...] = ("compile", "static_analysis", "sanitizer", "cbmc", "wp", "lean")
C_SUFFIXES = (".c",)
CPP_SUFFIXES = (".cpp", ".cc", ".cxx")


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Language(_M):
    name: Literal["c", "cpp"] = "c"
    standard: Literal["c17", "c11", "c99", "c++17", "c++20"] = "c17"

    @model_validator(mode="after")
    def _consistent(self) -> "Language":
        if (self.name == "cpp") != self.standard.startswith("c++"):
            raise ValueError(f"language {self.name!r} does not match standard {self.standard!r}")
        return self

    @property
    def is_cpp(self) -> bool:
        return self.name == "cpp"


class Param(_M):
    name: str
    type: str
    description: str = ""

    @field_validator("name")
    @classmethod
    def _ident(cls, v: str) -> str:
        if not C_IDENT_RE.match(v):
            raise ValueError(f"not a C identifier: {v!r}")
        return v


class ReturnValue(_M):
    value: str
    meaning: str


class Returns(_M):
    type: str
    description: str = ""
    values: list[ReturnValue] = Field(default_factory=list)


class Target(_M):
    function: str
    signature: str
    params: list[Param] = Field(default_factory=list)
    returns: Returns

    @field_validator("function")
    @classmethod
    def _ident(cls, v: str) -> str:
        if not C_IDENT_RE.match(v):
            raise ValueError(f"not a C identifier: {v!r}")
        return v


class Condition(_M):
    id: str
    text: str
    c_expr: str | None = None  # optional C expression mirrored in the harness


class SafetyProperty(_M):
    id: str
    text: str
    checks: list[CheckName] = Field(default_factory=list)  # CBMC checks that cover it


class Contract(_M):
    preconditions: list[Condition] = Field(default_factory=list)
    postconditions: list[Condition] = Field(default_factory=list)
    safety: list[SafetyProperty] = Field(default_factory=list)
    frame: list[str] = Field(
        default_factory=list, description="what the function must NOT modify, e.g. 'arr[0..len)'"
    )


class Sources(_M):
    editable: list[str] = Field(min_length=1, description="C files generation/repair may write")
    headers: list[str] = Field(default_factory=list, description="protected interface headers")
    include_dirs: list[str] = Field(default_factory=list)


class CompileConfig(_M):
    compiler: str = "clang"
    flags: list[str] = Field(default_factory=lambda: ["-Wall", "-Wextra", "-Wpedantic"])
    defines: list[str] = Field(default_factory=list)
    warnings_policy: Literal["report", "block"] = "report"
    timeout_s: float = Field(default=60, gt=0, le=3600)


class StaticAnalysisConfig(_M):
    policy: Literal["report", "block"] = "block"
    extra_checkers: list[str] = Field(default_factory=list)
    timeout_s: float = Field(default=120, gt=0, le=3600)


class TestCase(_M):
    name: str
    args: list[str] = Field(default_factory=list)
    stdin: str | None = None
    expect_exit: int = 0
    expect_stdout: str | None = None
    timeout_s: float | None = Field(default=None, gt=0, le=600)


class TestsConfig(_M):
    driver: str | None = Field(default=None, description="trusted C file with main()")
    cases: list[TestCase] = Field(default_factory=list)
    sanitizers: list[Literal["address", "undefined", "leak"]] = Field(
        default_factory=lambda: ["address", "undefined", "leak"]
    )
    timeout_s: float = Field(default=10, gt=0, le=600)
    memory_mb: int = Field(default=1024, ge=64, le=65536)
    max_output_bytes: int = Field(default=1024 * 1024, ge=1024, le=64 * 1024 * 1024)


class InputBound(_M):
    name: str
    min: str | None = None
    max: str | None = None
    note: str = ""


class VerificationConfig(_M):
    harness: str
    checks: list[CheckName] = Field(default_factory=list)
    unwind: int = Field(default=10, ge=1, le=100000)
    unwindset: dict[str, int] = Field(default_factory=dict)
    input_bounds: list[InputBound] = Field(default_factory=list)
    malloc_may_fail: bool = False
    object_bits: int | None = Field(default=None, ge=4, le=32)
    vacuity_check: bool = True
    coverage: bool = False
    timeout_s: float = Field(default=300, gt=0, le=86400)
    memory_mb: int = Field(default=4096, ge=128, le=262144)

    @field_validator("unwindset")
    @classmethod
    def _unwindset(cls, v: dict[str, int]) -> dict[str, int]:
        for k, b in v.items():
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_$]*\.\d+$", k):
                raise ValueError(f"unwindset key must look like 'function.N', got {k!r}")
            if not (1 <= int(b) <= 100000):
                raise ValueError(f"unwindset bound out of range for {k!r}")
        return v

    def effective_checks(self) -> list[str]:
        """Mandatory checks plus the spec's extra checks (stable order)."""
        out = list(MANDATORY_CHECKS)
        for c in self.checks:
            if c not in out:
                out.append(c)
        return out


class WPConfig(_M):
    """Frama-C/WP deductive verification (V2).

    The functional contract is the ACSL annotation of the target's prototype
    in the protected interface header (so it is part of the approved spec).
    WP always runs with ``-wp-rte`` (runtime-error guards become proof
    obligations) and smoke tests (inconsistent preconditions / dead code are
    detected); these cannot be switched off.
    """

    provers: list[str] = Field(default_factory=lambda: ["alt-ergo"], min_length=1)
    prover_timeout_s: float = Field(default=10, gt=0, le=3600)
    timeout_s: float = Field(default=600, gt=0, le=86400)
    memory_mb: int = Field(default=4096, ge=256, le=262144)
    require_termination: bool = Field(
        default=False, description="also prove termination (every loop needs a 'loop variant')"
    )

    @field_validator("provers")
    @classmethod
    def _provers(cls, v: list[str]) -> list[str]:
        for p in v:
            if not re.match(r"^[A-Za-z0-9_.:+-]{1,40}$", p):
                raise ValueError(f"invalid prover name {p!r}")
        return v


class LeanTheorem(_M):
    name: str
    statement: str = Field(description="fully qualified Lean name of a Prop defined in the trusted spec file")
    proof: str = Field(description="fully qualified Lean name the editable proofs must define")

    @field_validator("statement", "proof")
    @classmethod
    def _lean_name(cls, v: str) -> str:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_']*(\.[A-Za-z_][A-Za-z0-9_']*)*$", v):
            raise ValueError(f"not a Lean identifier: {v!r}")
        return v


class LeanVectors(_M):
    generator: str = Field(description="trusted Lean program printing test vectors from the model")
    driver: str = Field(description="trusted C/C++ driver reading vectors on stdin and checking the implementation")


class LeanConfig(_M):
    """Lean 4 model proofs (V2, optional).

    ``spec_file`` (trusted) defines a mathematical model of the target and the
    theorem *statements* as ``def name : Prop := ...``.  ``proofs`` (editable,
    may be written by the AI) must prove them.  Lean's kernel checks the
    proofs, and only the standard axioms are accepted.  ``vectors`` links the
    model to the C implementation by differential testing.  A Lean result is a
    statement about the model: it never upgrades the C verdict to PROVED.
    """

    spec_file: str
    proofs: list[str] = Field(min_length=1)
    theorems: list[LeanTheorem] = Field(min_length=1)
    vectors: LeanVectors | None = None
    timeout_s: float = Field(default=300, gt=0, le=86400)


class RepairConfig(_M):
    max_rounds: int = Field(default=5, ge=0, le=20)
    max_changed_lines: int = Field(default=200, ge=1, le=5000)


class Provenance(_M):
    origin: Literal["human", "ai-draft"] = "human"
    generator: str | None = None
    note: str = ""


class Spec(_M):
    leanc_spec: Literal[1] = 1
    spec_id: str
    version: int = Field(ge=1)
    title: str
    requirement: str = Field(description="original natural-language requirement (informative only)")
    language: Language = Field(default_factory=Language)
    target: Target
    contract: Contract = Field(default_factory=Contract)
    sources: Sources
    compile: CompileConfig = Field(default_factory=CompileConfig)
    static_analysis: StaticAnalysisConfig = Field(default_factory=StaticAnalysisConfig)
    tests: TestsConfig = Field(default_factory=TestsConfig)
    verification: VerificationConfig | None = None  # required when the cbmc gate is used
    wp: WPConfig | None = None
    lean: LeanConfig | None = None
    gates: list[GateName] = Field(default_factory=lambda: list(ALL_GATES))
    repair: RepairConfig = Field(default_factory=RepairConfig)
    provenance: Provenance = Field(default_factory=Provenance)

    @field_validator("spec_id")
    @classmethod
    def _spec_id(cls, v: str) -> str:
        if not SPEC_ID_RE.match(v):
            raise ValueError("spec_id must match ^[a-z0-9][a-z0-9._-]{0,63}$")
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> "Spec":
        def _n(p: str) -> str:
            try:
                return normalize_relpath(p)
            except Exception as exc:  # WorkspaceError
                raise ValueError(str(exc)) from None

        self.sources.editable = [_n(p) for p in self.sources.editable]
        self.sources.headers = [_n(p) for p in self.sources.headers]
        self.sources.include_dirs = [_n(p) for p in self.sources.include_dirs]
        for what, items in (("sources.editable", self.sources.editable), ("sources.headers", self.sources.headers)):
            dup = sorted({p for p in items if items.count(p) > 1})
            if dup:
                raise ValueError(f"duplicate paths in {what} (after normalisation): {dup}")
        if self.verification is not None:
            self.verification.harness = _n(self.verification.harness)
        if self.tests.driver:
            self.tests.driver = _n(self.tests.driver)
        if self.lean is not None:
            self.lean.spec_file = _n(self.lean.spec_file)
            self.lean.proofs = [_n(p) for p in self.lean.proofs]
            if self.lean.vectors is not None:
                self.lean.vectors.generator = _n(self.lean.vectors.generator)
                self.lean.vectors.driver = _n(self.lean.vectors.driver)
            for p in [self.lean.spec_file, *self.lean.proofs] + (
                [self.lean.vectors.generator] if self.lean.vectors else []
            ):
                if not p.endswith(".lean"):
                    raise ValueError(f"Lean files must end in .lean: {p}")
        suffixes = CPP_SUFFIXES if self.language.is_cpp else C_SUFFIXES
        for p in self.sources.editable:
            if not p.endswith(suffixes):
                kind = "C++ (.cpp/.cc/.cxx)" if self.language.is_cpp else "C (.c)"
                raise ValueError(f"editable sources must be {kind} files: {p}")
        if "cbmc" in self.gates and self.verification is None:
            raise ValueError("gate 'cbmc' requires a 'verification' section (CBMC harness)")
        if "cbmc" in self.gates and self.language.is_cpp:
            raise ValueError(
                "gate 'cbmc' is not available for C++: CBMC's C++ front end cannot parse modern C++ "
                "(no C++20-capable model checker is configured)"
            )
        if "wp" in self.gates and self.wp is None:
            self.wp = WPConfig()
        if "wp" in self.gates and self.language.is_cpp:
            raise ValueError("gate 'wp' (Frama-C) supports C only")
        if "lean" in self.gates and self.lean is None:
            raise ValueError("gate 'lean' requires a 'lean' section")
        protected = set(self.protected_paths())
        overlap = protected & set(self.editable_paths())
        if overlap:
            raise ValueError(f"files cannot be both editable and protected: {sorted(overlap)}")
        if len(set(self.gates)) != len(self.gates):
            raise ValueError("duplicate gates")
        if "sanitizer" in self.gates and not self.tests.driver:
            raise ValueError("gate 'sanitizer' requires tests.driver")
        if "sanitizer" in self.gates and not self.tests.cases:
            raise ValueError("gate 'sanitizer' requires at least one test case")
        names = [c.name for c in self.tests.cases]
        if len(set(names)) != len(names):
            raise ValueError("duplicate test case names")
        return self

    def protected_paths(self) -> list[str]:
        """Files that are part of the trusted specification (hash-locked on
        approval and never writable by generation or repair)."""
        out = [*([self.verification.harness] if self.verification else []), *self.sources.headers]
        if self.tests.driver:
            out.append(self.tests.driver)
        if self.lean is not None:
            out.append(self.lean.spec_file)
            if self.lean.vectors is not None:
                out += [self.lean.vectors.generator, self.lean.vectors.driver]
        return out

    def editable_paths(self) -> list[str]:
        """Everything generation / repair may write: C sources and Lean proofs."""
        return [*self.sources.editable, *(self.lean.proofs if self.lean else [])]
