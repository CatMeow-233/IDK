"""Prompts asking for prose: the root cause of one failure, and an explanation of a verification result."""

from __future__ import annotations

from leanc.foundation.models import FailureReport, VerificationResult
from leanc.llm.cpp_prompts import cpp_system
from leanc.llm.prompts.rendering import interface_summary, render_failure, render_verification
from leanc.llm.prompts.text import DEFAULT_LIMITS, Prompt, PromptLimits, finish_prompt, render_files
from leanc.specs.model import Spec

ANALYZE_SYSTEM = """\
You analyse failures found by Lean C's independent verification tools (CBMC counterexamples, sanitizer reports, \
static-analyzer paths, compiler errors) in C17 code. Explain, in plain prose (no JSON, no code changes, at most \
about 200 words): which input triggers the failure, which line misbehaves and why, and which clause of the spec is \
violated. Base every claim on the trace, inputs and code given; say explicitly when information is missing. Do not \
claim the code is correct or verified - only the tools decide that.
"""


def build_analyze_prompt(
    failure: FailureReport, sources: dict[str, str], spec: Spec, limits: PromptLimits = DEFAULT_LIMITS
) -> Prompt:
    files, _ = render_files(sources, limits.max_file_chars, limits.max_total_source_chars, label="source")
    user = f"""\
## Spec `{spec.spec_id}` v{spec.version}: {spec.title}
{interface_summary(spec)}

## Failure
{render_failure(failure, limits, sources)}

## Sources
{files}

Explain the root cause of this failure.
"""
    return finish_prompt(cpp_system(spec, "analyze", ANALYZE_SYSTEM), user, limits)


EXPLAIN_SYSTEM = """\
You explain Lean C verification results to developers. Be precise and never overstate: VERIFIED_BOUNDED means \
only that no violation of the checked properties exists within the approved harness, input constraints and unwind \
bound - it is not a proof; UNKNOWN, TIMEOUT and TOOL_ERROR mean no verdict. Never claim a property holds that the \
tools did not check, and never contradict the reported status. Plain prose, at most about 200 words: what the \
status means, what failed and where, and what a developer should look at next.
"""


def build_explain_prompt(result: VerificationResult, limits: PromptLimits = DEFAULT_LIMITS) -> Prompt:
    user = f"## Verification result\n{render_verification(result, limits)}\n\nExplain this result."
    return finish_prompt(EXPLAIN_SYSTEM, user, limits)
