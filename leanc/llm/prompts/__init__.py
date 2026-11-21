from __future__ import annotations

from leanc.llm.prompts.analysis import ANALYZE_SYSTEM, EXPLAIN_SYSTEM, build_analyze_prompt, build_explain_prompt
from leanc.llm.prompts.codegen import CODEGEN_SYSTEM, build_codegen_prompt
from leanc.llm.prompts.few_shot import FEW_SHOT_HARNESS_C, FEW_SHOT_HEADER_H, FEW_SHOT_SPEC_YAML, FEW_SHOT_TESTS_C
from leanc.llm.prompts.rendering import (
    render_diagnostic,
    render_diagnostics,
    render_failure,
    render_failures,
    render_history,
    render_location,
    render_verification,
)
from leanc.llm.prompts.repair import PATCH_FALLBACK_NOTE, REPAIR_SYSTEM, build_repair_prompt
from leanc.llm.prompts.schema import SCHEMA_RULES, spec_schema_summary
from leanc.llm.prompts.spec_draft import SPEC_DRAFT_SYSTEM, build_spec_draft_prompt
from leanc.llm.prompts.text import DEFAULT_LIMITS, Prompt, PromptLimits, fence, numbered_excerpt, truncate

__all__ = [
    "ANALYZE_SYSTEM",
    "CODEGEN_SYSTEM",
    "DEFAULT_LIMITS",
    "EXPLAIN_SYSTEM",
    "FEW_SHOT_HARNESS_C",
    "FEW_SHOT_HEADER_H",
    "FEW_SHOT_SPEC_YAML",
    "FEW_SHOT_TESTS_C",
    "PATCH_FALLBACK_NOTE",
    "Prompt",
    "PromptLimits",
    "REPAIR_SYSTEM",
    "SCHEMA_RULES",
    "SPEC_DRAFT_SYSTEM",
    "build_analyze_prompt",
    "build_codegen_prompt",
    "build_explain_prompt",
    "build_repair_prompt",
    "build_spec_draft_prompt",
    "fence",
    "numbered_excerpt",
    "render_diagnostic",
    "render_diagnostics",
    "render_failure",
    "render_failures",
    "render_history",
    "render_location",
    "render_verification",
    "spec_schema_summary",
    "truncate",
]
