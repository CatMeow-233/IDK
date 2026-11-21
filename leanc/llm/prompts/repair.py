from __future__ import annotations

from leanc.llm.base import RepairRequest
from leanc.llm.cpp_prompts import cpp_system
from leanc.llm.prompts.rendering import interface_summary, render_diagnostics, render_failures, render_history
from leanc.llm.prompts.text import DEFAULT_LIMITS, Prompt, PromptLimits, fence, finish_prompt, render_files, truncate
from leanc.llm.wp_prompts import wp_repair_note
from leanc.specs.model import Spec

REPAIR_SYSTEM = """\
You are the repair agent of Lean C, a spec-first verification pipeline for C17. The code you change is checked by \
independent tools - clang, the Clang Static Analyzer, ASan/UBSan/LSan tests and CBMC bounded model checking - \
against a human-approved, hash-locked spec and harness. Every candidate is policy-checked before it is verified; \
only a genuinely correct implementation passes.

HARD RULES (a violation gets the candidate rejected and wastes the round):
1. Only modify the editable source files listed in the request. NEVER modify protected files: the harness, the \
test driver, the interface headers, the spec, or leanc.h.
2. NEVER add __CPROVER_* built-ins, LEANC_* macros, or assume statements of any kind (__CPROVER_assume, \
LEANC_ASSUME, assume(), __builtin_assume, __assume, __attribute__((assume))).
3. NEVER delete, weaken or disable assertions or checks, and never remove error handling the spec requires.
4. NEVER make the function return a constant, ignore its inputs, or swallow/mask errors just to pass; never change \
the documented interface, parameter types or return codes.
5. NEVER suppress diagnostics: no #pragma clang/GCC diagnostic, no __attribute__((no_sanitize...)), no #line \
tricks, no redefinition of standard library or header macros, no main().
6. Make the MINIMAL change that fixes the ROOT CAUSE of the reported failures, keep correct behaviour everywhere \
else, stay within standard C17, and do not reformat unrelated code.

Answer with ONLY a JSON object (no prose, no markdown fences):
{"root_cause": "<one or two sentences>", "rationale": "<why the change fixes the root cause without weakening \
anything>", "files": {"<editable path>": "<COMPLETE new file content>"}}
Include only the files you change, each with its complete new content.
"""

PATCH_FALLBACK_NOTE = """\
NOTE: some editable files above were TRUNCATED in this request ({paths}). For those, do not answer with "files"; \
answer instead with {{"root_cause": "...", "rationale": "...", "patch": "<unified diff with a/ and b/ prefixes \
against the current files>"}}."""

LEAN_PROOF_RULES = (
    "## Lean proof rules\n"
    "The Lean files listed as editable must prove the theorem STATEMENTS defined in the protected Lean spec "
    "file (each `def <name> : Prop`), under exactly the proof names given in the spec. Use only core Lean 4 "
    "/ Std tactics (omega, simp, decide, induction, cases ...). Forbidden: sorry, admit, axiom, unsafe, "
    "partial, opaque, extern/implemented_by, native_decide or other native evaluation, set_option (except "
    "maxHeartbeats/maxRecDepth), #eval/run_cmd and other metaprogramming, new instances/notation/macros, "
    "imports other than Init, Std, the spec module and other proof modules. Never redefine model functions."
)

REPAIR_ANSWER_INSTRUCTION = (
    "Fix the root cause with a minimal change, obeying the hard rules. Answer with ONLY the JSON object "
    '{"root_cause", "rationale", "files": {path: full new content}}.'
)

PROTECTED_BEYOND_SPEC_FILES = ("the spec file", "leanc.h")


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _fenced_c_or(content: str | None, label: str, limits: PromptLimits, absent: str) -> str:
    return fence(truncate(content, limits.max_context_file_chars, label=label), "c") if content else absent


def _files_quotable_in_failure_excerpts(req: RepairRequest) -> dict[str, str]:
    spec = req.spec
    quotable = dict(req.sources)
    if req.harness and spec.verification is not None:
        quotable.setdefault(spec.verification.harness, req.harness)
    if req.tests and spec.tests.driver:
        quotable.setdefault(spec.tests.driver, req.tests)
    return quotable


def _task_parts(req: RepairRequest, sources: str, limits: PromptLimits) -> list[str]:
    spec = req.spec
    editable = spec.editable_paths()
    protected = sorted(set(spec.protected_paths()))
    parts = [
        f"## Repair round {req.round} for spec `{spec.spec_id}` v{spec.version}: {spec.title}",
        "## Requirement (informative)\n" + fence(truncate(req.requirement, limits.max_requirement_chars, label="requirement")),
        "## Approved spec (protected)\n" + fence(truncate(req.spec_yaml, limits.max_spec_chars, label="spec"), "yaml"),
        "## Interface\n" + interface_summary(spec),
        "## Editable files (the ONLY files you may change)\n" + _bullets(editable),
        "## Protected files (never change)\n" + _bullets([*protected, *PROTECTED_BEYOND_SPEC_FILES]),
        "## Current editable sources\n" + sources,
    ]
    missing = [path for path in editable if path not in req.sources]
    if missing:
        parts.append("Editable files that do not exist yet: " + ", ".join(missing))
    return parts


def _tool_evidence_parts(req: RepairRequest, limits: PromptLimits) -> list[str]:
    failures = render_failures(req.failures, limits, _files_quotable_in_failure_excerpts(req))
    return [
        "## Failures reported by the verification tools\n" + failures,
        "## Diagnostics\n" + render_diagnostics(req.diagnostics, limits),
        "## Earlier repair rounds (do not repeat rejected or reverted ideas)\n" + render_history(req.history, limits),
    ]


def _trusted_file_parts(req: RepairRequest, limits: PromptLimits) -> list[str]:
    per_file, total = limits.max_context_file_chars, limits.max_total_context_chars
    headers, _ = render_files(req.headers, per_file, total, label="header")
    parts = [
        "## Trusted CBMC harness (protected, read-only)\n" + _fenced_c_or(req.harness, "harness", limits, "(not available)"),
        "## Trusted test driver (protected, read-only)\n" + _fenced_c_or(req.tests, "tests", limits, "(none)"),
        "## Protected headers (read-only)\n" + headers,
    ]
    if req.protected_context:
        other_files, _ = render_files(req.protected_context, per_file, total, label="protected")
        parts.append("## Other protected files (read-only; never change them)\n" + other_files)
    return parts


def _proof_rule_parts(spec: Spec) -> list[str]:
    parts = [LEAN_PROOF_RULES] if spec.lean is not None else []
    wp_note = wp_repair_note(spec)
    if wp_note:
        parts.append(wp_note)
    return parts


def build_repair_prompt(req: RepairRequest, limits: PromptLimits = DEFAULT_LIMITS) -> Prompt:
    spec = req.spec
    sources, truncated = render_files(
        dict(req.sources), limits.max_file_chars, limits.max_total_source_chars, label="source"
    )
    parts = _task_parts(req, sources, limits)
    parts += _tool_evidence_parts(req, limits)
    parts += _trusted_file_parts(req, limits)
    parts += _proof_rule_parts(spec)
    if truncated:
        parts.append(PATCH_FALLBACK_NOTE.format(paths=", ".join(truncated)))
    parts.append(REPAIR_ANSWER_INSTRUCTION)
    return finish_prompt(cpp_system(spec, "repair", REPAIR_SYSTEM), "\n\n".join(parts), limits)
