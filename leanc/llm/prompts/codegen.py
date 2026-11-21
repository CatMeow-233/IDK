"""Prompt for implementing the editable files of an approved spec."""

from __future__ import annotations

from leanc.llm.base import CodeGenRequest
from leanc.llm.cpp_prompts import cpp_system
from leanc.llm.prompts.rendering import interface_summary
from leanc.llm.prompts.text import DEFAULT_LIMITS, Prompt, PromptLimits, fence, finish_prompt, render_files, truncate

CODEGEN_SYSTEM = """\
You are the code generator of Lean C, a spec-first pipeline for C17. Your output is NOT trusted: it will be \
compiled with clang (C17, -Wall -Wextra -Wpedantic), analysed by the Clang Static Analyzer, run against \
human-written tests under ASan/UBSan/LSan, and checked by CBMC against a human-approved harness. Only genuinely \
correct code passes; a policy checker rejects forbidden constructs before anything runs.

Rules:
1. Implement exactly the approved interface: the function signature and semantics given by the spec and the \
protected headers. #include the headers; never re-declare, change or add to them, and never change the signature, \
the parameter types or the documented return codes.
2. Standard C17 and the C standard library only: no compiler-specific extensions, no inline assembly, and no main() \
(the harness and the test driver provide main).
3. Never use __CPROVER_* built-ins, LEANC_* macros, assume statements (__CPROVER_assume, assume(), \
__builtin_assume, __assume), #pragma diagnostic suppression, or __attribute__((no_sanitize...)).
4. Handle every documented error case and every input the preconditions allow: no out-of-bounds access, NULL \
dereference, signed overflow, invalid shift, division by zero, uninitialised read or memory leak.
5. Never return constants or ignore inputs to satisfy tests; implement the specified behaviour.
6. Each file must be complete and compile on its own.

Answer with ONLY a JSON object (no prose, no markdown fences):
{"files": {"<target path>": "<complete file content>"}, "notes": "<short implementation notes>"}
The "files" object must contain exactly the requested target paths and nothing else.
"""


def build_codegen_prompt(req: CodeGenRequest, limits: PromptLimits = DEFAULT_LIMITS) -> Prompt:
    spec = req.spec
    headers, _ = render_files(req.headers, limits.max_context_file_chars, limits.max_total_context_chars, label="header")
    targets = "\n".join(f"- {t}" for t in req.targets) or "(none)"
    user = f"""\
## Approved spec `{spec.spec_id}` v{spec.version}: {spec.title}
{fence(truncate(req.spec_yaml, limits.max_spec_chars, label="spec"), "yaml")}

## Interface to implement
{interface_summary(spec)}
C standard: {spec.language.standard}; include directories: {", ".join(spec.sources.include_dirs) or "(none)"}

## Protected headers (read-only; #include them, do not copy or change them)
{headers}

## Files to write (exactly these paths)
{targets}

Answer with ONLY the JSON object {{"files": {{...}}, "notes": "..."}}.
"""
    return finish_prompt(cpp_system(spec, "codegen", CODEGEN_SYSTEM), user, limits)
