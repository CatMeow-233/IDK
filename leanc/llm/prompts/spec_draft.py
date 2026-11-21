from __future__ import annotations

from leanc.llm.base import SpecDraftRequest
from leanc.llm.prompts.few_shot import FEW_SHOT_HARNESS_C, FEW_SHOT_HEADER_H, FEW_SHOT_SPEC_YAML, FEW_SHOT_TESTS_C
from leanc.llm.prompts.schema import spec_schema_summary
from leanc.llm.prompts.text import DEFAULT_LIMITS, Prompt, PromptLimits, fence, finish_prompt, truncate

UNTITLED_PLACEHOLDER = "(choose a short descriptive title)"

SPEC_DRAFT_SYSTEM = """\
You are the specification assistant of Lean C, a spec-first pipeline that generates C17 code with an AI and \
verifies it with independent tools (clang, the Clang Static Analyzer, ASan/UBSan/LSan tests, CBMC bounded model \
checking).

Turn a natural-language requirement into a CANDIDATE Lean C spec (YAML), a CBMC harness draft and a runtime test \
driver draft. Your draft is UNTRUSTED: it has no effect until a human reviews the spec, the harness and the tests \
against the requirement and approves them with `leanc spec approve`. Write it so that review is easy: explicit \
preconditions, explicit error behaviour for every invalid input, no hidden assumptions, and harness assumptions \
(LEANC_ASSUME) that mirror ONLY the stated preconditions - over-constraining the harness hides bugs.

{schema}

Guidelines:
- Use the given spec_id, version 1, and the conventional layout: src/<name>.c (editable), include/<name>.h \
(protected header), harness/<function>_harness.c, tests/test_<function>.c; include_dirs: [include].
- Prefer returning error codes over undefined behaviour; document every return value.
- Choose input bounds small enough for CBMC (e.g. arrays up to 8 elements) and set unwind above the largest loop \
count they allow.
- Do NOT implement the function: that is done later, from the approved spec.
- Omit `provenance`: Lean C marks every draft as `origin: ai-draft` itself.
- Put the proposed interface header (include/<name>.h content) and any open questions for the reviewer in "notes".

Answer with ONLY a JSON object (no prose, no markdown fences):
{{"spec_yaml": "<the spec as YAML text>", "harness_c": "<harness C source>", "tests_c": "<test driver C source>", \
"notes": "<proposed header, assumptions, open questions>"}}
"""


def build_spec_draft_prompt(req: SpecDraftRequest, limits: PromptLimits = DEFAULT_LIMITS) -> Prompt:
    system = SPEC_DRAFT_SYSTEM.format(schema=spec_schema_summary())
    title = req.title or UNTITLED_PLACEHOLDER
    user = f"""\
## Requirement (natural language; copy it verbatim into `requirement`)
{fence(truncate(req.requirement, limits.max_requirement_chars, label="requirement"))}

## Parameters
- spec_id: {req.spec_id}
- title: {title}
- language: C, standard {req.language}

## Example: a complete, human-approved Lean C spec for a DIFFERENT requirement (format reference only)
### spec.yaml
{fence(FEW_SHOT_SPEC_YAML, "yaml")}
### include/safe_array.h
{fence(FEW_SHOT_HEADER_H, "c")}
### harness/safe_array_get_harness.c
{fence(FEW_SHOT_HARNESS_C, "c")}
### tests/test_safe_array_get.c
{fence(FEW_SHOT_TESTS_C, "c")}

## Task
Write the candidate spec, harness and test driver for the requirement above (spec_id `{req.spec_id}`).
Remember: this draft is UNTRUSTED until a human approves it.
Answer with ONLY the JSON object {{"spec_yaml", "harness_c", "tests_c", "notes"}}.
"""
    return finish_prompt(system, user, limits)
