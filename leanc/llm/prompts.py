"""System and user prompts for every provider method.

Design rules:

* **The model is a proposer, never a judge.**  Prompts say so explicitly: code
  and patches are compiled, analysed, tested and model-checked by independent
  tools; spec drafts are UNTRUSTED until a human approves them.
* **Hard rules are stated, then enforced elsewhere.**  The repair prompt lists
  what the policy checker (:mod:`leanc.repair.policy`) rejects - protected
  files, CBMC built-ins, assumptions, assertion deletion, trivialised targets.
  Stating them saves rounds; it is not what makes the pipeline safe.
* **Bounded context.**  Every section is capped (:class:`PromptLimits`); long
  files are cut in the middle with an explicit marker so the model knows it is
  not seeing everything (and is told to answer with a diff instead).
* **No secrets.**  The final text of every prompt goes through
  :func:`leanc.foundation.redact.redact`.

All builders are pure and deterministic, so prompts can be unit-tested and
diffed between versions.
"""

from __future__ import annotations

import json
import re
import types
import typing
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel

from leanc.foundation.models import Diagnostic, FailureReport, VerificationResult
from leanc.foundation.redact import redact
from leanc.llm.base import CodeGenRequest, RepairRequest, SpecDraftRequest
from leanc.llm.cpp_prompts import cpp_system
from leanc.llm.wp_prompts import wp_repair_note
from leanc.specs.model import ALL_GATES, MANDATORY_CHECKS, SPEC_ID_RE, Spec


# ------------------------------------------------------------------ limits / containers
@dataclass(frozen=True)
class PromptLimits:
    """Context caps (characters).  Defaults keep a prompt well below ~100k tokens."""

    max_requirement_chars: int = 8_000
    max_spec_chars: int = 30_000
    max_file_chars: int = 40_000  # per file
    max_total_source_chars: int = 120_000  # all editable sources together
    max_context_file_chars: int = 20_000  # per protected context file (harness, tests, headers)
    max_diagnostics: int = 30
    max_failures: int = 8
    max_trace_steps: int = 40
    max_inputs: int = 40
    max_history: int = 8
    max_item_chars: int = 2_000  # one diagnostic / history entry / message
    max_prompt_chars: int = 400_000  # hard cap for the whole user prompt


DEFAULT_LIMITS = PromptLimits()


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str

    def messages(self) -> list[dict[str, str]]:
        """OpenAI-style chat messages."""
        return [{"role": "system", "content": self.system}, {"role": "user", "content": self.user}]

    @property
    def size(self) -> int:
        return len(self.system) + len(self.user)


# ------------------------------------------------------------------ text helpers
def truncate(text: str | None, limit: int, *, label: str = "text") -> str:
    """Cut ``text`` to about ``limit`` characters, keeping head and tail and
    inserting an explicit marker (never a silent cut)."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 0:
        return f"[{label} omitted: {len(text)} characters]"
    head_len = (limit * 2) // 3
    tail_len = limit - head_len
    omitted = len(text) - head_len - tail_len
    tail = text[len(text) - tail_len :] if tail_len > 0 else ""
    return f"{text[:head_len]}\n... [{label} truncated: {omitted} characters omitted] ...\n{tail}"


def fence(content: str, lang: str = "") -> str:
    """Markdown code block whose fence cannot be closed by the content."""
    longest = max((len(m) for m in re.findall(r"`+", content or "")), default=0)
    ticks = "`" * max(3, longest + 1)
    body = content if (content or "").endswith("\n") else (content or "") + "\n"
    return f"{ticks}{lang}\n{body}{ticks}"


def _lang_for(path: str) -> str:
    if path.endswith((".c", ".h")):
        return "c"
    if path.endswith((".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx")):
        return "cpp"
    if path.endswith((".yaml", ".yml")):
        return "yaml"
    if path.endswith(".json"):
        return "json"
    return ""


def _finish(system: str, user: str, limits: PromptLimits) -> Prompt:
    return Prompt(system=redact(system.strip() + "\n"), user=redact(truncate(user.strip() + "\n", limits.max_prompt_chars, label="prompt")))


def numbered_excerpt(content: str, line: int, radius: int = 4) -> str:
    """Lines ``line-radius .. line+radius`` of ``content`` with line numbers
    (the marked line is prefixed with ``>``)."""
    lines = content.splitlines()
    if not lines or line < 1 or line > len(lines):
        return ""
    lo, hi = max(1, line - radius), min(len(lines), line + radius)
    width = len(str(hi))
    out = []
    for n in range(lo, hi + 1):
        mark = ">" if n == line else " "
        out.append(f"{mark}{n:>{width}} | {lines[n - 1]}")
    return "\n".join(out)


# ------------------------------------------------------------------ schema summary
def _meta_constraints(metadata: Iterable[Any]) -> str:
    parts = []
    for m in metadata:
        for attr, sym in (("ge", ">="), ("gt", ">"), ("le", "<="), ("lt", "<")):
            v = getattr(m, attr, None)
            if v is not None:
                parts.append(f"{sym}{v}")
        v = getattr(m, "min_length", None)
        if v is not None:
            parts.append(f"at least {v} item(s)")
    return f" ({', '.join(parts)})" if parts else ""


def _model_of(ann: Any) -> type[BaseModel] | None:
    """The pydantic model nested in ``ann`` (directly, in a list or Optional)."""
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return ann
    for a in get_args(ann):
        m = _model_of(a)
        if m is not None:
            return m
    return None


def _type_name(ann: Any) -> str:
    if ann is type(None):
        return "null"
    origin = get_origin(ann)
    args = get_args(ann)
    if origin is Literal:
        return " | ".join(str(a) for a in args)
    if origin in (typing.Union, types.UnionType):
        return " | ".join(_type_name(a) for a in args)
    if origin is list:
        inner = args[0] if args else Any
        if get_origin(inner) is Literal:
            return f"list of ({_type_name(inner)})"
        return f"list of {_type_name(inner)}"
    if origin is dict:
        return f"map {_type_name(args[0])} -> {_type_name(args[1])}" if len(args) == 2 else "map"
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return "object"
    if isinstance(ann, type):
        return {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}.get(ann.__name__, ann.__name__)
    return str(ann)


def _default_repr(field: Any) -> str:
    try:
        default = field.get_default(call_default_factory=True)
    except Exception:  # pragma: no cover - defensive
        return ""
    if isinstance(default, BaseModel):
        return ""
    if default is None:
        return " = null"
    try:
        s = json.dumps(default, ensure_ascii=False)
    except TypeError:
        s = repr(default)
    return f" = {s}" if len(s) <= 80 else ""


def _describe_model(model: type[BaseModel], indent: int, out: list[str]) -> None:
    pad = "  " * indent
    for name, field in model.model_fields.items():
        ann = field.annotation
        line = f"{pad}{name}: {_type_name(ann)}{_meta_constraints(field.metadata)}"
        line += " (required)" if field.is_required() else _default_repr(field)
        if field.description:
            line += f"  # {field.description}"
        out.append(line)
        nested = _model_of(ann)
        if nested is not None:
            if get_origin(ann) is list:
                out.append(f"{pad}  - (each item):")
                _describe_model(nested, indent + 2, out)
            else:
                _describe_model(nested, indent + 1, out)


SCHEMA_RULES: tuple[str, ...] = (
    f"spec_id must match {SPEC_ID_RE.pattern}; version starts at 1 and is bumped for every approved change.",
    "requirement keeps the original natural-language requirement verbatim (informative only; no tool reads it).",
    "target.signature is the exact C prototype declared in the protected interface header.",
    "sources.editable: the .c files the code generator / repair agent may write - and nothing else.",
    "Protected files (hash-locked on approval, never writable by AI): verification.harness, sources.headers, "
    "tests.driver. No file may be both editable and protected.",
    f"verification.checks lists EXTRA CBMC checks; always enabled whatever the spec says: {', '.join(MANDATORY_CHECKS)}.",
    "verification.unwind must exceed the largest loop iteration count the harness allows (unwinding assertions "
    "are checked: an insufficient bound yields UNKNOWN, never success); unwindset keys look like 'function.N'.",
    "verification.input_bounds documents the bounds the harness enforces with LEANC_ASSUME.",
    f"gates is a subset of {list(ALL_GATES)}; the 'sanitizer' gate requires tests.driver and at least one "
    "tests.cases entry.",
    "tests.cases: each case runs the test driver (built with ASan/UBSan/LSan) with `args` and expects exit "
    "status expect_exit (and exact stdout if expect_stdout is set).",
    "Harness: a C file with main() that #includes \"leanc.h\" and the interface header, obtains inputs from "
    "nondet_*() functions, constrains them ONLY by the stated preconditions with LEANC_ASSUME(cond), calls the "
    "target, checks postconditions with assert(), and has at least one LEANC_REACH(\"label\") probe after the call "
    "(ideally one per branch) so a vacuous harness is detected.",
    "Test driver: a C file with main(int argc, char **argv) that runs the case named argv[1] and returns 0 on success.",
    "Unknown keys are rejected anywhere in the spec.",
)


@lru_cache(maxsize=1)
def spec_schema_summary() -> str:
    """Field outline of :class:`leanc.specs.model.Spec`, derived from the
    pydantic model itself (so it cannot drift from the validator), followed by
    the semantic rules the validator and pipeline enforce."""
    lines = ["Lean C spec schema (YAML mapping; field: type (constraints) = default):"]
    _describe_model(Spec, 1, lines)
    lines.append("")
    lines.append("Rules:")
    lines.extend(f"- {r}" for r in SCHEMA_RULES)
    return "\n".join(lines)


# ------------------------------------------------------------------ few-shot example
#: The approved ``examples/safe_array_get`` spec, harness and tests, embedded so
#: the few-shot does not depend on the examples directory being installed.
FEW_SHOT_SPEC_YAML = """\
leanc_spec: 1
spec_id: safe-array-get
version: 1
title: Safe integer array read
requirement: |
  Implement a safe integer-array read. Out-of-range indices must return an error
  instead of reading invalid memory.
language: {name: c, standard: c17}
target:
  function: safe_array_get
  signature: "int safe_array_get(const int *arr, size_t len, size_t idx, int *out)"
  params:
    - {name: arr, type: "const int *", description: "array of len readable ints, or NULL"}
    - {name: len, type: size_t, description: "number of elements in arr"}
    - {name: idx, type: size_t, description: "index to read (any value)"}
    - {name: out, type: "int *", description: "where the element is written, or NULL"}
  returns:
    type: int
    description: status code
    values:
      - {value: "SAFE_ARRAY_OK (0)", meaning: "success, *out == arr[idx]"}
      - {value: "SAFE_ARRAY_ERR_NULL (-1)", meaning: "arr or out is NULL; nothing dereferenced"}
      - {value: "SAFE_ARRAY_ERR_RANGE (-2)", meaning: "idx >= len; no memory read, *out untouched"}
contract:
  preconditions:
    - {id: PRE1, text: "if arr != NULL then arr points to at least len readable ints", c_expr: "len <= MAX_LEN"}
  postconditions:
    - {id: POST1, text: "arr, out non-NULL and idx < len => returns 0 and *out == arr[idx]"}
    - {id: POST2, text: "idx >= len => returns -2 and *out is unchanged"}
    - {id: POST3, text: "arr == NULL or out == NULL => returns -1"}
    - {id: POST4, text: "the return value is always one of 0, -1, -2"}
  safety:
    - {id: SAFE1, text: "no out-of-bounds memory access", checks: [bounds, pointer]}
    - {id: SAFE2, text: "no NULL dereference", checks: [pointer]}
    - {id: SAFE3, text: "no undefined behaviour", checks: [signed-overflow, pointer-overflow, undefined-shift]}
  frame:
    - "arr[0..len) is never modified"
sources:
  editable: [src/safe_array.c]
  headers: [include/safe_array.h]
  include_dirs: [include]
compile:
  flags: [-Wall, -Wextra, -Wpedantic]
  warnings_policy: report
static_analysis:
  policy: block
tests:
  driver: tests/test_safe_array_get.c
  cases:
    - {name: normal, args: [normal]}
    - {name: out_of_range, args: [out_of_range]}
    - {name: "null", args: ["null"]}
verification:
  harness: harness/safe_array_get_harness.c
  checks: [unsigned-overflow, conversion]
  unwind: 9
  input_bounds:
    - {name: len, min: "0", max: "8", note: "array sizes up to MAX_LEN=8 are explored"}
    - {name: idx, note: "unconstrained (full size_t range)"}
  timeout_s: 120
gates: [compile, static_analysis, sanitizer, cbmc]
repair:
  max_rounds: 5
"""

FEW_SHOT_HEADER_H = """\
#ifndef SAFE_ARRAY_H
#define SAFE_ARRAY_H
#include <stddef.h>
#define SAFE_ARRAY_OK 0
#define SAFE_ARRAY_ERR_NULL (-1)
#define SAFE_ARRAY_ERR_RANGE (-2)
int safe_array_get(const int *arr, size_t len, size_t idx, int *out);
#endif
"""

FEW_SHOT_HARNESS_C = """\
/* Trusted CBMC harness for safe_array_get (human-reviewed). */
#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "safe_array.h"

#define MAX_LEN 8

int main(void)
{
    int storage[MAX_LEN];
    int snapshot[MAX_LEN];
    for (size_t k = 0; k < MAX_LEN; k++) {
        storage[k] = nondet_int();
        snapshot[k] = storage[k];
    }
    size_t len = nondet_size_t();
    size_t idx = nondet_size_t();          /* idx is completely unconstrained */
    LEANC_ASSUME(len <= MAX_LEN);          /* PRE1 */
    const int *arr = nondet_bool() ? NULL : storage;
    int out_cell = nondet_int();
    int out_before = out_cell;
    int *out = nondet_bool() ? NULL : &out_cell;

    int rc = safe_array_get(arr, len, idx, out);
    LEANC_REACH("after_call");

    assert(rc == SAFE_ARRAY_OK || rc == SAFE_ARRAY_ERR_NULL || rc == SAFE_ARRAY_ERR_RANGE); /* POST4 */
    if (arr == NULL || out == NULL) {
        LEANC_REACH("null_input");
        assert(rc == SAFE_ARRAY_ERR_NULL);                                                  /* POST3 */
    } else if (idx >= len) {
        LEANC_REACH("out_of_range");
        assert(rc == SAFE_ARRAY_ERR_RANGE);                                                 /* POST2 */
        assert(out_cell == out_before);
    } else {
        LEANC_REACH("in_range");
        assert(rc == SAFE_ARRAY_OK);                                                        /* POST1 */
        assert(out_cell == storage[idx]);
    }
    for (size_t k = 0; k < MAX_LEN; k++) {
        assert(storage[k] == snapshot[k]);                                                  /* frame */
    }
    return 0;
}
"""

FEW_SHOT_TESTS_C = """\
/* Trusted runtime tests for safe_array_get (run under ASan/UBSan). */
#include <stdio.h>
#include <string.h>

#include "safe_array.h"

static int check(int cond, const char *what)
{
    if (!cond) {
        fprintf(stderr, "FAILED: %s\\n", what);
        return 1;
    }
    return 0;
}

static int test_normal(void)
{
    int a[5] = {10, 20, 30, 40, 50};
    int out = -1;
    int f = 0;
    f |= check(safe_array_get(a, 5, 0, &out) == SAFE_ARRAY_OK && out == 10, "first element");
    f |= check(safe_array_get(a, 5, 4, &out) == SAFE_ARRAY_OK && out == 50, "last element");
    return f;
}

static int test_out_of_range(void)
{
    int a[3] = {1, 2, 3};
    int out = 77;
    int f = 0;
    f |= check(safe_array_get(a, 3, 3, &out) == SAFE_ARRAY_ERR_RANGE, "idx == len");
    f |= check(safe_array_get(a, 3, (size_t)-1, &out) == SAFE_ARRAY_ERR_RANGE, "idx == SIZE_MAX");
    f |= check(safe_array_get(a, 0, 0, &out) == SAFE_ARRAY_ERR_RANGE, "empty array");
    f |= check(out == 77, "out untouched");
    return f;
}

static int test_null(void)
{
    int a[2] = {1, 2};
    int out = 0;
    int f = 0;
    f |= check(safe_array_get(NULL, 2, 0, &out) == SAFE_ARRAY_ERR_NULL, "NULL arr");
    f |= check(safe_array_get(a, 2, 0, NULL) == SAFE_ARRAY_ERR_NULL, "NULL out");
    return f;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <case>\\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "normal") == 0) return test_normal();
    if (strcmp(argv[1], "out_of_range") == 0) return test_out_of_range();
    if (strcmp(argv[1], "null") == 0) return test_null();
    fprintf(stderr, "unknown case %s\\n", argv[1]);
    return 2;
}
"""


# ------------------------------------------------------------------ renderers (shared with the mock)
def render_location(loc: Any) -> str:
    return loc.short() if loc is not None else "?"


def render_diagnostic(d: Diagnostic, limits: PromptLimits = DEFAULT_LIMITS) -> str:
    rule = f" {d.rule_id}" if d.rule_id else ""
    where = f" at {render_location(d.location)}" if d.location else ""
    text = f"- [{d.tool}] {d.severity}{rule}{where}: {d.message}"
    for step in d.path[:10]:
        text += f"\n    path: {render_location(step.location)}: {step.message}"
    if len(d.path) > 10:
        text += f"\n    path: ... {len(d.path) - 10} more step(s)"
    return truncate(text, limits.max_item_chars, label="diagnostic")


def render_failure(
    f: FailureReport,
    limits: PromptLimits = DEFAULT_LIMITS,
    sources: dict[str, str] | None = None,
) -> str:
    """Deterministic, model-readable description of one failure.  With
    ``sources``, a numbered excerpt around the failure location is added."""
    lines = [f"- id: {f.id}", f"  kind: {f.kind}; detected by: {f.tool}"]
    if f.property_id:
        lines.append(f"  property: {f.property_id}")
    lines.append(f"  description: {truncate(f.description, limits.max_item_chars, label='description')}")
    if f.location:
        lines.append(f"  location: {render_location(f.location)}")
    if f.inputs:
        shown = f.inputs[: limits.max_inputs]
        inputs = ", ".join(f"{i.name}={i.value}" for i in shown)
        if len(f.inputs) > len(shown):
            inputs += f", ... ({len(f.inputs) - len(shown)} more)"
        lines.append(f"  inputs: {truncate(inputs, limits.max_item_chars, label='inputs')}")
    if f.call_stack:
        lines.append(f"  call stack: {' -> '.join(f.call_stack)}")
    if f.trace:
        steps = f.trace[-limits.max_trace_steps :]
        skipped = len(f.trace) - len(steps)
        lines.append(f"  trace (last {len(steps)} step(s){f', {skipped} earlier omitted' if skipped else ''}):")
        for s in steps:
            loc = f" {render_location(s.location)}" if s.location else ""
            lines.append(f"    [{s.kind}]{loc} {truncate(s.text, 300, label='step')}".rstrip())
    if f.explanation:
        lines.append(f"  explanation: {truncate(f.explanation, limits.max_item_chars, label='explanation')}")
    if sources and f.location and f.location.file in sources and f.location.line:
        ex = numbered_excerpt(sources[f.location.file], f.location.line)
        if ex:
            lines.append(f"  code at {f.location.file}:{f.location.line}:")
            lines.extend("    " + x for x in ex.splitlines())
    return "\n".join(lines)


def render_failures(
    failures: list[FailureReport], limits: PromptLimits = DEFAULT_LIMITS, sources: dict[str, str] | None = None
) -> str:
    if not failures:
        return "(none)"
    shown = failures[: limits.max_failures]
    out = [render_failure(f, limits, sources) for f in shown]
    if len(failures) > len(shown):
        out.append(f"- ... {len(failures) - len(shown)} more failure(s) omitted")
    return "\n".join(out)


def render_diagnostics(diags: list[Diagnostic], limits: PromptLimits = DEFAULT_LIMITS) -> str:
    if not diags:
        return "(none)"
    shown = diags[: limits.max_diagnostics]
    out = [render_diagnostic(d, limits) for d in shown]
    if len(diags) > len(shown):
        out.append(f"- ... {len(diags) - len(shown)} more diagnostic(s) omitted")
    return "\n".join(out)


def render_history(history: list[dict], limits: PromptLimits = DEFAULT_LIMITS) -> str:
    if not history:
        return "(this is the first round)"
    shown = history[-limits.max_history :]
    out = []
    if len(history) > len(shown):
        out.append(f"- ... {len(history) - len(shown)} earlier round(s) omitted")
    for h in shown:
        out.append("- " + truncate(json.dumps(h, ensure_ascii=False, sort_keys=True, default=str), limits.max_item_chars, label="round"))
    return "\n".join(out)


def render_verification(result: VerificationResult, limits: PromptLimits = DEFAULT_LIMITS) -> str:
    """Compact, deterministic textual form of a verification result."""
    st = result.status.value if hasattr(result.status, "value") else str(result.status)
    head = f"status: {st}"
    if result.spec_id:
        head += f"\nspec: {result.spec_id}" + (f" v{result.spec_version}" if result.spec_version else "")
    lines = [head]
    if result.reasons:
        lines.append("reasons:")
        lines.extend(f"- {truncate(r, limits.max_item_chars, label='reason')}" for r in result.reasons)
    if result.stages:
        lines.append("stages:")
        for s in result.stages:
            oc = s.outcome.value if hasattr(s.outcome, "value") else str(s.outcome)
            req = "" if s.required else " (optional)"
            summ = f": {truncate(s.summary, limits.max_item_chars, label='summary')}" if s.summary else ""
            lines.append(f"- {s.stage}{req}: {oc}{summ}")
    failures = result.all_failures()
    lines.append(f"failures ({len(failures)}):")
    lines.append(render_failures(failures, limits))
    if result.assumptions:
        lines.append("assumptions (scope of a bounded result):")
        lines.extend(f"- {a}" for a in result.assumptions)
    if result.limitations:
        lines.append("limitations:")
        lines.extend(f"- {x}" for x in result.limitations)
    return "\n".join(lines)


def _render_files(
    files: dict[str, str], per_file: int, total: int, *, label: str
) -> tuple[str, list[str]]:
    """Render files as fenced blocks under a shared budget; return the text and
    the paths that had to be truncated."""
    out: list[str] = []
    truncated: list[str] = []
    budget = total
    for path in sorted(files):
        content = files[path] or ""
        limit = max(0, min(per_file, budget))
        shown = truncate(content, limit, label=f"{label} {path}")
        if shown != content:
            truncated.append(path)
        budget -= min(len(content), limit)
        out.append(f"### {path}\n{fence(shown, _lang_for(path))}")
    return ("\n\n".join(out) if out else "(none)"), truncated


# ------------------------------------------------------------------ spec draft
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
    title = req.title or "(choose a short descriptive title)"
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
    return _finish(system, user, limits)


# ------------------------------------------------------------------ code generation
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


def _interface_summary(spec: Spec) -> str:
    t = spec.target
    lines = [f"function: {t.function}", f"signature: {t.signature}"]
    for p in t.params:
        lines.append(f"param {p.name}: {p.type}" + (f" - {p.description}" if p.description else ""))
    lines.append(f"returns: {t.returns.type}" + (f" - {t.returns.description}" if t.returns.description else ""))
    for v in t.returns.values:
        lines.append(f"  {v.value}: {v.meaning}")
    c = spec.contract
    for label, items in (("pre", c.preconditions), ("post", c.postconditions)):
        for cond in items:
            lines.append(f"{label} {cond.id}: {cond.text}")
    for s in c.safety:
        lines.append(f"safety {s.id}: {s.text}")
    for fr in c.frame:
        lines.append(f"frame: {fr}")
    return "\n".join(lines)


def build_codegen_prompt(req: CodeGenRequest, limits: PromptLimits = DEFAULT_LIMITS) -> Prompt:
    spec = req.spec
    headers, _ = _render_files(req.headers, limits.max_context_file_chars, limits.max_context_file_chars * 4, label="header")
    targets = "\n".join(f"- {t}" for t in req.targets) or "(none)"
    user = f"""\
## Approved spec `{spec.spec_id}` v{spec.version}: {spec.title}
{fence(truncate(req.spec_yaml, limits.max_spec_chars, label="spec"), "yaml")}

## Interface to implement
{_interface_summary(spec)}
C standard: {spec.language.standard}; include directories: {", ".join(spec.sources.include_dirs) or "(none)"}

## Protected headers (read-only; #include them, do not copy or change them)
{headers}

## Files to write (exactly these paths)
{targets}

Answer with ONLY the JSON object {{"files": {{...}}, "notes": "..."}}.
"""
    return _finish(cpp_system(spec, "codegen", CODEGEN_SYSTEM), user, limits)


# ------------------------------------------------------------------ repair
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


def build_repair_prompt(req: RepairRequest, limits: PromptLimits = DEFAULT_LIMITS) -> Prompt:
    spec = req.spec
    editable = spec.editable_paths()  # C sources and Lean proof files
    sources, truncated = _render_files(
        {k: v for k, v in req.sources.items()}, limits.max_file_chars, limits.max_total_source_chars, label="source"
    )
    missing = [p for p in editable if p not in req.sources]
    protected = sorted(set(spec.protected_paths()))
    headers, _ = _render_files(req.headers, limits.max_context_file_chars, limits.max_context_file_chars * 4, label="header")
    harness = (
        fence(truncate(req.harness, limits.max_context_file_chars, label="harness"), "c") if req.harness else "(not available)"
    )
    tests = fence(truncate(req.tests, limits.max_context_file_chars, label="tests"), "c") if req.tests else "(none)"
    parts = [
        f"## Repair round {req.round} for spec `{spec.spec_id}` v{spec.version}: {spec.title}",
        "## Requirement (informative)\n" + fence(truncate(req.requirement, limits.max_requirement_chars, label="requirement")),
        "## Approved spec (protected)\n" + fence(truncate(req.spec_yaml, limits.max_spec_chars, label="spec"), "yaml"),
        "## Interface\n" + _interface_summary(spec),
        "## Editable files (the ONLY files you may change)\n" + "\n".join(f"- {p}" for p in editable),
        "## Protected files (never change)\n" + "\n".join(f"- {p}" for p in [*protected, "the spec file", "leanc.h"]),
        "## Current editable sources\n" + sources,
    ]
    if missing:
        parts.append("Editable files that do not exist yet: " + ", ".join(missing))
    # Numbered excerpts are shown for failures located in editable sources and
    # in the trusted harness / tests (an assertion failure points at the harness).
    excerpt_files = dict(req.sources)
    if req.harness:
        if spec.verification is not None:
            excerpt_files.setdefault(spec.verification.harness, req.harness)
    if req.tests and spec.tests.driver:
        excerpt_files.setdefault(spec.tests.driver, req.tests)
    parts += [
        "## Failures reported by the verification tools\n" + render_failures(req.failures, limits, excerpt_files),
        "## Diagnostics\n" + render_diagnostics(req.diagnostics, limits),
        "## Earlier repair rounds (do not repeat rejected or reverted ideas)\n" + render_history(req.history, limits),
        "## Trusted CBMC harness (protected, read-only)\n" + harness,
        "## Trusted test driver (protected, read-only)\n" + tests,
        "## Protected headers (read-only)\n" + headers,
    ]
    if req.protected_context:
        ctx_text, _ = _render_files(
            req.protected_context, limits.max_context_file_chars, limits.max_context_file_chars * 4, label="protected"
        )
        parts.append("## Other protected files (read-only; never change them)\n" + ctx_text)
    if spec.lean is not None:
        parts.append(
            "## Lean proof rules\n"
            "The Lean files listed as editable must prove the theorem STATEMENTS defined in the protected Lean spec "
            "file (each `def <name> : Prop`), under exactly the proof names given in the spec. Use only core Lean 4 "
            "/ Std tactics (omega, simp, decide, induction, cases ...). Forbidden: sorry, admit, axiom, unsafe, "
            "partial, opaque, extern/implemented_by, native_decide or other native evaluation, set_option (except "
            "maxHeartbeats/maxRecDepth), #eval/run_cmd and other metaprogramming, new instances/notation/macros, "
            "imports other than Init, Std, the spec module and other proof modules. Never redefine model functions."
        )

    if wp_note := wp_repair_note(spec):
        parts.append(wp_note)
    if truncated:
        parts.append(PATCH_FALLBACK_NOTE.format(paths=", ".join(truncated)))
    parts.append(
        "Fix the root cause with a minimal change, obeying the hard rules. Answer with ONLY the JSON object "
        '{"root_cause", "rationale", "files": {path: full new content}}.'
    )
    return _finish(cpp_system(spec, "repair", REPAIR_SYSTEM), "\n\n".join(parts), limits)


# ------------------------------------------------------------------ counterexample analysis
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
    files, _ = _render_files(sources, limits.max_file_chars, limits.max_total_source_chars, label="source")
    user = f"""\
## Spec `{spec.spec_id}` v{spec.version}: {spec.title}
{_interface_summary(spec)}

## Failure
{render_failure(failure, limits, sources)}

## Sources
{files}

Explain the root cause of this failure.
"""
    return _finish(cpp_system(spec, "analyze", ANALYZE_SYSTEM), user, limits)


# ------------------------------------------------------------------ explanation
EXPLAIN_SYSTEM = """\
You explain Lean C verification results to developers. Be precise and never overstate: VERIFIED_BOUNDED means \
only that no violation of the checked properties exists within the approved harness, input constraints and unwind \
bound - it is not a proof; UNKNOWN, TIMEOUT and TOOL_ERROR mean no verdict. Never claim a property holds that the \
tools did not check, and never contradict the reported status. Plain prose, at most about 200 words: what the \
status means, what failed and where, and what a developer should look at next.
"""


def build_explain_prompt(result: VerificationResult, limits: PromptLimits = DEFAULT_LIMITS) -> Prompt:
    user = f"## Verification result\n{render_verification(result, limits)}\n\nExplain this result."
    return _finish(EXPLAIN_SYSTEM, user, limits)


__all__ = [
    "DEFAULT_LIMITS",
    "FEW_SHOT_HARNESS_C",
    "FEW_SHOT_SPEC_YAML",
    "FEW_SHOT_TESTS_C",
    "Prompt",
    "PromptLimits",
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
    "render_verification",
    "spec_schema_summary",
    "truncate",
]
