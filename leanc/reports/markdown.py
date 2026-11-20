"""Human-readable Markdown reports (``verification.md`` and ``report.md``).

Rules this renderer follows, and why:

* **Never read more favourably than the data.**  Every verdict is printed with
  :data:`STATUS_MEANING`, which states precisely what the status does and does
  not establish (``VERIFIED_BOUNDED`` is bounded and is never ``PROVED``).  The
  general limitations of the method are always printed, whatever the result.
* **Attribute findings to the detecting tool.**  The same defect may be seen by
  the compiler, the static analyzer, a sanitizer, the test oracle and CBMC;
  readers (and the experiments) need to know *which* independent tool saw what.
* **Treat tool and provider text as untrusted.**  Tool output, test output and
  LLM-supplied root causes / rationales are escaped (inline) or fenced
  (multi-line) so they cannot inject headings, tables, links or fake verdicts.
  Table cells escape pipes and newlines.
* **Deterministic and redacted.**  Output depends only on the input objects
  (no clock, no environment, stable ordering), and the final text is passed
  through :func:`leanc.core.redact.redact`.
"""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath
from typing import Iterable

from leanc.core.models import (
    ApprovalCheck,
    Diagnostic,
    FailureReport,
    Location,
    RepairRound,
    RunRecord,
    StageResult,
    ToolInvocation,
    TraceStep,
    VerificationResult,
)
from leanc.core.redact import redact
from leanc.core.status import Status, exit_code, is_success
from leanc.reports.json_report import detecting_tools, is_finding, sort_tools

#: What each status means -- and, just as importantly, what it does not mean.
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

#: Printed in every report, whatever the outcome.
ALWAYS_LIMITATIONS: tuple[str, ...] = (
    "Sanitizers (ASan/UBSan/LSan) only cover executed paths: they can only report defects on the paths "
    "executed by the trusted test cases; untested inputs and paths may still be defective.",
    "CBMC results are bounded: they hold only for the approved harness, its input constraints, the unwind "
    "bound and the checked property classes, under CBMC's C semantics and memory model; a bounded result "
    "is never a proof (PROVED).",
)

# Restatements of ALWAYS_LIMITATIONS (e.g. the controller's standard wording) are
# folded into them instead of being printed twice.
_RESTATES_ALWAYS = (
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

MAX_FINDINGS_PER_TOOL = 50
MAX_COUNTEREXAMPLES = 20
TRACE_EXCERPT_STEPS = 15
MAX_CELL = 300
MAX_LINE = 200


# =========================================================================== escaping
# Backslash, backticks, asterisks, ``<`` that could open an HTML tag / autolink,
# and underscores at word boundaries (intraword ``_`` never emphasises in GFM).
_INLINE_RE = re.compile(r"[\\`*]|<(?=[A-Za-z/!?])|(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])")
# Block constructs that could start where escaped text starts a line (e.g. the
# content of a list item): ATX headings, block quotes, bullets, ordered-list
# markers, setext underlines / thematic breaks and tilde fences.
_LEADING_BLOCK_RES = (
    re.compile(r"^(#{1,6})(?=\s|$)"),
    re.compile(r"^(>)"),
    re.compile(r"^([-+])(?=\s|$)"),
    re.compile(r"^(=+|-+)(?=\s*$)"),
    re.compile(r"^(~~~)"),
)
_LEADING_ORDERED_RE = re.compile(r"^(\d{1,9})([.)])(?=\s|$)")
_NEWLINES_RE = re.compile(r"\s*[\r\n]+\s*")
_BACKTICK_RUN_RE = re.compile(r"`+")


def _flat(text: object) -> str:
    return _NEWLINES_RE.sub(" ", "" if text is None else str(text)).strip()


def _trunc(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _esc(text: object, limit: int | None = None) -> str:
    """Escape untrusted text for inline use (single line, no markup)."""
    s = _flat(text)
    if limit:
        s = _trunc(s, limit)
    s = _INLINE_RE.sub(lambda m: "\\" + m.group(0), s)
    s = s.replace("](", "\\](")  # no inline links / images
    for rx in _LEADING_BLOCK_RES:
        s = rx.sub(r"\\\1", s)
    s = _LEADING_ORDERED_RE.sub(r"\1\\\2", s)
    return s


def _code(text: object, limit: int | None = None) -> str:
    """Inline code span that is safe for any content (backticks included)."""
    s = _flat(text)
    if limit:
        s = _trunc(s, limit)
    if not s:
        return "-"
    longest = max((len(m) for m in _BACKTICK_RUN_RE.findall(s)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if (s[0] == "`" or s[-1] == "`" or (s[0] == " " and s[-1] == " ")) else ""
    return f"{fence}{pad}{s}{pad}{fence}"


def _cell(text: str) -> str:
    """Make already-escaped inline markdown safe inside a GFM table cell."""
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    s = s.replace("|", "\\|").replace("\n", "<br>")
    return s or " "


def _fence(text: str, lang: str = "text") -> list[str]:
    """Fenced block whose fence cannot be closed by the content."""
    body = (text or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    longest = max((len(m) for m in _BACKTICK_RUN_RE.findall(body)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{lang}", *body.split("\n"), fence]


def _table(header: list[str], rows: Iterable[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_cell(c) for c in r) + " |")
    return out


def _yes(b: bool | None) -> str:
    return "unknown" if b is None else ("yes" if b else "no")


def _dur(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds:.2f} s"


def _loc(loc: Location | None) -> str:
    return _code(loc.short()) if loc else "unknown location"


def _status(st: Status | None) -> str:
    return _code(st.value) if st else "-"


def _tool_heading(tool: str) -> str:
    label = TOOL_LABELS.get(tool)
    return f"{_code(tool)} ({label})" if label else _code(tool)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


# =========================================================================== sections
def _verdict(status: Status, reasons: list[str], *, label: str = "Status") -> list[str]:
    out = [f"**{label}: {_code(status.value)}** (exit code {exit_code(status)})", ""]
    out += [f"Meaning: {STATUS_MEANING.get(status, 'no description available for this status')}", ""]
    if status is Status.SPEC_UNAPPROVED:
        out += ["**No result in this report is a verification result against a trusted specification.**", ""]
    if status is Status.PROVED:
        out += [
            "**PROVED is relative to the approved ACSL contract: review that the contract states what you need.**",
            "",
        ]
    if reasons:
        out.append("Reasons:")
        out.append("")
        out += [f"- {_esc(r)}" for r in reasons]
    else:
        out.append("Reasons: none recorded.")
    out.append("")
    return out


def _scope(vr: VerificationResult) -> list[str]:
    out = ["## Scope & assumptions", ""]
    if is_success(vr.status):
        out += [f"The {_code(vr.status.value)} verdict holds **only** within this scope:", ""]
    else:
        out += ["Conditions under which the gates were run (no success is claimed):", ""]
    if vr.assumptions:
        out += [f"- {_esc(a)}" for a in vr.assumptions]
    else:
        out.append("- none recorded")
    out.append("")
    if vr.source_hashes:
        out += ["Sources examined:", ""]
        out += _table(["file", "sha256"], ([_code(p), _code(h)] for p, h in sorted(vr.source_hashes.items())))
        out.append("")
    return out


def _approval_lines(a: ApprovalCheck | None) -> list[str]:
    if a is None:
        return ["- Approval: **no approval check recorded**: treat the specification as untrusted"]
    if a.approved:
        return [
            f"- Approval: **approved** by {_code(a.approved_by or 'unknown')} at {_esc(a.approved_at or 'unknown time')}"
            f"; signature valid: {_yes(a.signature_valid)}"
        ]
    out = [
        "- Approval: **NOT approved**: nothing in this report was formally verified against a trusted "
        f"specification (signature valid: {_yes(a.signature_valid)})"
    ]
    out += [f"  - {_esc(r)}" for r in a.reasons] or ["  - no reason recorded"]
    return out


def _spec_section(vr: VerificationResult) -> list[str]:
    out = ["## Specification & approval", ""]
    a = vr.approval
    spec_id = vr.spec_id or (a.spec_id if a else None)
    version = vr.spec_version if vr.spec_version is not None else (a.spec_version if a else None)
    out.append(f"- Spec id: {_code(spec_id or 'unknown')}, version {_esc(version if version is not None else 'unknown')}")
    out.append(f"- Spec hash (sha256): {_code(vr.spec_hash or (a.spec_hash if a else None) or 'unknown')}")
    out += _approval_lines(a)
    out.append("")
    if a and a.protected_files:
        out += ["Protected (trusted) files, hashes as approved:", ""]
        out += _table(["file", "sha256"], ([_code(p), _code(h)] for p, h in sorted(a.protected_files.items())))
        out.append("")
    return out


def _environment(vr: VerificationResult) -> list[str]:
    out = ["## Environment", ""]
    env = vr.environment
    if env is None:
        return out + ["Environment information was not recorded.", ""]
    out.append(f"- Lean C {_esc(env.leanc_version)}; Python {_esc(env.python)}; {_esc(env.platform)}")
    note = f": {_esc(env.sandbox_note)}" if env.sandbox_note else ""
    out.append(f"- Sandbox: {_code(env.sandbox)}{note}")
    if env.sandbox == "none":
        out.append("- **No sandbox was active**: untrusted programs were not isolated (or were not executed).")
    out.append("")
    if env.tools:
        out += _table(
            ["tool", "available", "version", "path", "note"],
            (
                [_code(t.name), _yes(t.available), _esc(t.version) if t.version else "-", _code(t.path or "-"), _esc(t.note or "")]
                for t in env.tools
            ),
        )
        out.append("")
    return out


def _gate_rows(stages: list[StageResult]) -> Iterable[list[str]]:
    for s in stages:
        name = _code(s.stage) + ("" if s.required else " (optional)")
        outcome = s.outcome.value if s.outcome.value == "PASS" else f"**{s.outcome.value}**"
        tools = ", ".join(_code(t) for t in detecting_tools(s)) or "-"
        yield [name, outcome, _status(s.status), tools, _dur(s.duration_s), _esc(s.summary, MAX_CELL)]


def _gates(vr: VerificationResult, intro: str | None = None) -> list[str]:
    out = ["## Gates", ""]
    if intro:
        out += [intro, ""]
    if not vr.stages:
        return out + ["No gate was run.", ""]
    out += _table(["stage", "outcome", "status", "detecting tools", "duration", "summary"], _gate_rows(vr.stages))
    out += ["", "*detecting tools* = tools that reported a failure or an error/warning diagnostic in that gate.", ""]
    return out


def _diag_key(tool: str, loc: Location | None, message: str) -> tuple:
    loc = loc or Location()
    return (tool, loc.file, loc.line, loc.column, message)


def _findings(vr: VerificationResult) -> list[str]:
    out = ["## Findings by detecting tool", ""]
    groups: dict[str, list[tuple[str, StageResult, FailureReport | Diagnostic]]] = {}
    notes = 0
    for s in vr.stages:
        covered = {_diag_key(f.tool, f.location, f.description) for f in s.failures}
        for f in s.failures:
            groups.setdefault(f.tool, []).append(("failure", s, f))
        for d in s.diagnostics:
            if not is_finding(d):
                notes += 1
                continue
            if _diag_key(d.tool, d.location, d.message) in covered:
                continue
            groups.setdefault(d.tool, []).append(("diagnostic", s, d))
    if not groups:
        out += [
            "No tool reported a failure or warning. This alone does not mean the code is correct: "
            "see the Verdict and the Gates table for what was actually established.",
            "",
        ]
    for tool in sort_tools(groups):
        items = groups[tool]
        nf = sum(1 for k, _, _ in items if k == "failure")
        nd = len(items) - nf
        counts = [_plural(nf, "failure")] + ([_plural(nd, "non-blocking diagnostic")] if nd else [])
        out += [f"### {_tool_heading(tool)}: {', '.join(counts)}", ""]
        for kind, s, obj in items[:MAX_FINDINGS_PER_TOOL]:
            if isinstance(obj, FailureReport):
                out.append(f"- **{_esc(obj.kind)}** at {_loc(obj.location)}: {_esc(obj.description, 500)}")
                meta = [f"stage {_code(s.stage)}"]
                if obj.property_id:
                    meta.append(f"property {_code(obj.property_id)}")
                meta.append(f"id {_code(obj.id)}")
                if obj.raw_ref:
                    meta.append(f"raw output {_code(obj.raw_ref)}")
                out.append("  - " + "; ".join(meta))
            else:
                out.append(f"- {_esc(obj.severity)} (non-blocking) at {_loc(obj.location)}: {_esc(obj.message, 500)}")
                meta = [f"stage {_code(s.stage)}"]
                if obj.rule_id:
                    meta.append(f"rule {_code(obj.rule_id)}")
                out.append("  - " + "; ".join(meta))
        if len(items) > MAX_FINDINGS_PER_TOOL:
            out.append(f"- … {len(items) - MAX_FINDINGS_PER_TOOL} more (see verification.json)")
        out.append("")
    if notes:
        out += [f"{_plural(notes, 'note-level diagnostic')} (context for other findings) omitted; see verification.json.", ""]
    return out


def _is_counterexample(f: FailureReport) -> bool:
    return f.kind == "counterexample" or bool(f.inputs) or bool(f.call_stack) or (
        f.kind in ("runtime", "test-oracle") and bool(f.trace)
    )


def _trace_line(idx: int, step: TraceStep) -> str:
    where = f" {step.location.short()}" if step.location else ""
    return _trunc(f"#{idx} {step.kind}{where}: {_flat(step.text)}", MAX_LINE)


def _counterexample(n: int, stage: StageResult, f: FailureReport) -> list[str]:
    title = _code(f.property_id) if f.property_id else _esc(f.description, 80)
    out = [f"### {n}. {title} ({_code(f.tool)})", ""]
    out.append(f"- Property: {_code(f.property_id) if f.property_id else 'not named'}: {_esc(f.description, 500)}")
    out.append(f"- Location: {_loc(f.location)}")
    out.append(f"- Detected by {_code(f.tool)} in stage {_code(stage.stage)}; failure id {_code(f.id)}")
    out.append("")
    if f.inputs:
        out += ["Inputs:", ""]
        out += _table(["name", "value", "type"], ([_code(i.name), _code(i.value), _code(i.type) if i.type else "-"] for i in f.inputs))
        out.append("")
    else:
        out += ["Inputs: none recorded.", ""]
    if f.call_stack:
        out += ["Call stack (outermost first):", ""]
        out += [f"{k}. {_code(frame, MAX_LINE)}" for k, frame in enumerate(f.call_stack, 1)]
        out.append("")
    if f.trace:
        total = len(f.trace)
        start = max(0, total - TRACE_EXCERPT_STEPS)
        shown = f"last {total - start} of {total} steps" if start else f"all {total} steps"
        out += [f"Trace excerpt ({shown}):", ""]
        out += _fence("\n".join(_trace_line(i + 1, f.trace[i]) for i in range(start, total)))
        out.append("")
    if f.explanation:
        out += [f"Explanation: {_esc(f.explanation, 1000)}", ""]
    out += [f"Raw tool output: {_code(f.raw_ref) if f.raw_ref else 'not recorded'}", ""]
    return out


def _counterexamples(vr: VerificationResult) -> list[str]:
    out = ["## Counterexamples", ""]
    items = [(s, f) for s in vr.stages for f in s.failures if _is_counterexample(f)]
    if not items:
        return out + ["No counterexample or failing execution was recorded.", ""]
    out += [
        "Concrete evidence: CBMC counterexamples and failing sanitizer / test-oracle executions "
        "(paths are relative to the verification directory).",
        "",
    ]
    for n, (s, f) in enumerate(items[:MAX_COUNTEREXAMPLES], 1):
        out += _counterexample(n, s, f)
    if len(items) > MAX_COUNTEREXAMPLES:
        out += [f"… {len(items) - MAX_COUNTEREXAMPLES} more (see verification.json).", ""]
    return out


def _limitations(vr: VerificationResult | None, extra: Iterable[str] = ()) -> list[str]:
    out = ["## Limitations", ""]
    items: list[str] = list(ALWAYS_LIMITATIONS)
    for item in [*extra, *(vr.limitations if vr else [])]:
        if item in items or any(rx.search(item.strip()) for rx in _RESTATES_ALWAYS):
            continue
        items.append(item)
    out += [f"- {_esc(i)}" for i in items]
    out.append("")
    return out


def _invocation(stage: str, k: int, inv: ToolInvocation) -> list[str]:
    facts = [f"exit {inv.exit_code if inv.exit_code is not None else '-'}"]
    if inv.signal:
        facts.append(f"signal {inv.signal}")
    if inv.timed_out:
        facts.append("**timed out**")
    if inv.output_truncated:
        facts.append("output truncated")
    facts += [_dur(inv.duration_s), f"sandbox {_code(inv.sandbox)}"]
    if inv.stdout_path:
        facts.append(f"stdout {_code(inv.stdout_path)}")
    if inv.stderr_path:
        facts.append(f"stderr {_code(inv.stderr_path)}")
    out = [f"{_code(stage)} #{k}, {_code(inv.tool)}: {'; '.join(facts)}", ""]
    if inv.note:
        out += [f"Note: {_esc(inv.note, 500)}", ""]
    cmd = shlex.join(inv.argv) if inv.argv else "(argv not recorded)"
    out += _fence(f"cd {shlex.quote(inv.cwd)} && {cmd}" if inv.cwd else cmd, "sh")
    out.append("")
    return out


def _reproduce(vr: VerificationResult | None, rerun: str | None = None) -> list[str]:
    out = ["## Reproduce", ""]
    if rerun:
        out += ["Re-run the workflow (flags other than the provider were not recorded):", ""]
        out += _fence(rerun, "sh")
        out.append("")
    invs = [(s.stage, k, inv) for s in (vr.stages if vr else []) for k, inv in enumerate(s.invocations, 1)]
    if not invs:
        return out + ["No tool invocation was recorded.", ""]
    out += [
        "Tool invocations in execution order (argv as run, before any sandbox wrapping; secrets redacted; "
        "log paths are relative to the verification directory):",
        "",
    ]
    for stage, k, inv in invs:
        out += _invocation(stage, k, inv)
    return out


def _header_vr(vr: VerificationResult) -> list[str]:
    spec = f"{vr.spec_id or 'unknown spec'}" + (f" v{vr.spec_version}" if vr.spec_version is not None else "")
    return [
        f"# Lean C verification report: {_esc(spec)}",
        "",
        f"- Run: {_code(vr.run_id)}",
        f"- Started: {_esc(vr.started_at)}; finished: {_esc(vr.finished_at or 'not recorded')}",
        "",
    ]


_FENCE_OPEN_RE = re.compile(r"^(`{3,})[^`]*$")


def _finish(lines: list[str]) -> str:
    """Join lines, collapsing runs of blank lines outside fenced blocks (fenced
    content is reproduced verbatim), then redact."""
    out: list[str] = []
    fence: str | None = None
    for line in lines:
        if fence is None:
            m = _FENCE_OPEN_RE.match(line)
            if m:
                fence = m.group(1)
            elif line == "" and (not out or out[-1] == ""):
                continue
        elif line == fence:
            fence = None
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return redact("\n".join(out) + "\n")


# =========================================================================== public
def _coverage(vr: VerificationResult | None) -> list[str]:
    """CBMC ``--cover location`` results (spec verification.coverage: true)."""
    if vr is None:
        return []
    st = vr.stage("cbmc")
    cov = st.details.get("coverage") if st is not None else None
    if not isinstance(cov, dict) or not cov:
        return []
    out = [
        "## Coverage under the harness assumptions",
        "",
        "Location coverage of the editable sources reachable under the approved harness (CBMC `--cover location`). "
        "Uncovered lines can indicate over-constrained assumptions or dead code; coverage is advisory and never "
        "changes the verdict.",
        "",
        "| file | goals | covered | uncovered lines |",
        "|---|---|---|---|",
    ]
    for f in sorted(cov):
        c = cov[f] if isinstance(cov[f], dict) else {}
        unc = c.get("uncovered_lines") or []
        shown = ", ".join(str(x) for x in unc[:40]) + (" ..." if len(unc) > 40 else "")
        out.append(f"| {_cell(_code(f))} | {_cell(str(c.get('goals', '?')))} | {_cell(str(c.get('covered', '?')))} | {_cell(shown or '-')} |")
    return out + [""]


def render_verification_md(vr: VerificationResult) -> str:
    """Render one verification as Markdown (``verification.md``)."""
    lines = _header_vr(vr)
    lines += ["## Verdict", ""] + _verdict(vr.status, vr.reasons)
    lines += _scope(vr)
    lines += _spec_section(vr)
    lines += _environment(vr)
    lines += _gates(vr)
    lines += _findings(vr)
    lines += _counterexamples(vr)
    lines += _coverage(vr)
    lines += _limitations(vr)
    lines += _reproduce(vr)
    return _finish(lines)


def _round_dir(rr: RunRecord, vr: VerificationResult) -> str | None:
    """Directory (relative to the run dir) holding ``vr``'s artifacts, when the
    run record says so (repair rounds record their verification path)."""
    for r in rr.repair_rounds:
        if r.verification_path and vr.run_id.endswith(f"-r{r.round}"):
            return str(PurePosixPath(r.verification_path).parent) + "/"
    return None


def _policy_text(r: RepairRound) -> str:
    if r.policy is None:
        return "not checked"
    if r.policy.allowed:
        return "allowed"
    return f"**rejected** ({_plural(len(r.policy.violations), 'violation')})"


def _transition(r: RepairRound) -> str:
    after = _status(r.status_after) if r.status_after else "not re-verified"
    return f"{_status(r.status_before)} → {after}"


def _ids(ids: list[str]) -> str:
    return ", ".join(_code(i) for i in ids) if ids else "none"


def _repair_history(rr: RunRecord) -> list[str]:
    out = ["## Repair history", ""]
    if rr.provider is not None and not rr.provider.real_llm:
        out += [f"> **{MOCK_DISCLAIMER}.** The proposals below were replayed from fixture files.", ""]
    if not rr.repair_rounds:
        return out + ["No repair round was run.", ""]
    rejected = sum(1 for r in rr.repair_rounds if r.policy is not None and not r.policy.allowed)
    reverted = sum(1 for r in rr.repair_rounds if r.reverted)
    out += [
        f"{_plural(len(rr.repair_rounds), 'round')}; {rejected} rejected by policy; {reverted} reverted. "
        "Root causes and rationales are provider-supplied and unverified; only the re-verification "
        "status counts.",
        "",
    ]
    out += _table(
        ["round", "provider", "status before → after", "+/- lines", "policy", "reverted", "note"],
        (
            [
                str(r.round),
                _code(r.provider),
                _transition(r),
                f"+{r.lines_added} / -{r.lines_removed}",
                _policy_text(r),
                _yes(r.reverted),
                _esc(r.note, 160),
            ]
            for r in rr.repair_rounds
        ),
    )
    out.append("")
    for r in rr.repair_rounds:
        out += [f"### Round {r.round} (provider {_code(r.provider)})", ""]
        out.append(f"- Status: {_transition(r)}")
        out.append(f"- Root cause (provider-supplied): {_esc(r.root_cause, 500) or 'none given'}")
        files = ", ".join(_code(f) for f in r.files_changed) or "none"
        out.append(f"- Files changed: {files} (+{r.lines_added} / -{r.lines_removed} lines)")
        if r.policy is None:
            out.append("- Policy decision: not checked")
        elif r.policy.allowed:
            out.append("- Policy decision: allowed")
        else:
            out.append(f"- Policy decision: **rejected**, {_plural(len(r.policy.violations), 'violation')}:")
            for v in r.policy.violations:
                where = ""
                if v.file:
                    where = " at " + _code(f"{v.file}:{v.line}" if v.line is not None else v.file)
                out.append(f"  - {_code(v.rule)}{where}: {_esc(v.message, 300)}")
        out.append(f"- Fixed failures: {_ids(r.fixed_failures)}")
        out.append(f"- Introduced failures: {_ids(r.introduced_failures)}")
        out.append(f"- Reverted: {_yes(r.reverted)}")
        out.append(f"- Patch: {_code(r.patch_path) if r.patch_path else 'none'}")
        if r.verification_path:
            out.append(f"- Verification: {_code(r.verification_path)}")
        if r.candidate_hash:
            out.append(f"- Candidate hash: {_code(r.candidate_hash)}")
        if r.note:
            out.append(f"- Note: {_esc(r.note, 500)}")
        out.append("")
        if r.rationale:
            out += ["Rationale (provider-supplied, unverified):", ""]
            out += _fence(_trunc(r.rationale, 2000))
            out.append("")
    return out


def _provider_section(rr: RunRecord) -> list[str]:
    out = ["## AI provider & generation", ""]
    p = rr.provider
    if p is not None:
        kind = "real LLM" if p.real_llm else f"not an LLM ({MOCK_DISCLAIMER})"
        out.append(f"- Provider: {_code(p.name)}, model {_code(p.model or 'none')}: {kind}")
    else:
        out.append("- Provider: none recorded")
    out.append(
        "- AI-generated and AI-repaired code is untrusted: it is judged only by the independent tools in the "
        "gates, never by the provider."
    )
    g = rr.generation
    if g is None:
        out += ["- Generation: no code was generated in this run", ""]
        return out
    gp = f" (provider {_code(g.provider.name)}, real LLM: {_yes(g.provider.real_llm)})"
    out.append(f"- Generation: {_plural(len(g.files), 'file')} generated{gp}")
    if g.note:
        out.append(f"- Generation note: {_esc(g.note, 500)}")
    out.append("")
    if g.files:
        out += _table(["generated file", "sha256 of generated content"], ([_code(f), _code(h)] for f, h in sorted(g.files.items())))
        out.append("")
    return out


def _initial_section(iv: VerificationResult) -> list[str]:
    out = ["## Initial verification", ""]
    out += [
        f"Before repair: {_code(iv.status.value)} (run {_code(iv.run_id)}). "
        f"{STATUS_MEANING.get(iv.status, '')}",
        "",
    ]
    if iv.stages:
        out += _table(["stage", "outcome", "status", "detecting tools", "duration", "summary"], _gate_rows(iv.stages))
        out.append("")
    fails = iv.all_failures()
    if fails:
        out += ["Failures found before repair:", ""]
        for f in fails[:MAX_FINDINGS_PER_TOOL]:
            inputs = ", ".join(f"{i.name}={i.value}" for i in f.inputs)
            tail = f"; inputs {_code(inputs, MAX_LINE)}" if inputs else ""
            out.append(f"- {_code(f.tool)} {_esc(f.kind)} at {_loc(f.location)}: {_esc(f.description, 300)} (id {_code(f.id)}{tail})")
        if len(fails) > MAX_FINDINGS_PER_TOOL:
            out.append(f"- … {len(fails) - MAX_FINDINGS_PER_TOOL} more")
        out.append("")
    return out


def _header_rr(rr: RunRecord) -> list[str]:
    spec = _code(rr.spec_id or "unknown")
    if rr.spec_path:
        spec += f" ({_code(rr.spec_path)})"
    return [
        f"# Lean C run report: leanc {_esc(rr.command)}",
        "",
        f"- Run: {_code(rr.run_id)}",
        f"- Command: {_code('leanc ' + rr.command)}",
        f"- Spec: {spec}",
        f"- Project root: {_code(rr.project_root)}",
        f"- Lean C version: {_esc(rr.leanc_version)}",
        f"- Started: {_esc(rr.started_at)}; finished: {_esc(rr.finished_at or 'not recorded')}",
        "",
    ]


def render_run_md(rr: RunRecord) -> str:
    """Render a whole workflow run (``report.md``): verdict, provider disclaimer,
    final verification, initial verification, repair history, reproduction."""
    vr = rr.verification
    mock = rr.provider is not None and not rr.provider.real_llm
    lines = _header_rr(rr)
    if mock:
        lines += [
            f"> **Provider disclaimer: {MOCK_DISCLAIMER}.** Generated code and repair proposals in this run "
            "were replayed from fixtures; nothing here measures the performance of any LLM.",
            "",
        ]

    # ------------------------------------------------------------- verdict
    lines += ["## Verdict", ""]
    if vr is not None and vr.status is rr.status:
        lines += _verdict(rr.status, vr.reasons, label="Workflow status")
        if rr.messages:
            lines += ["Workflow messages:", ""] + [f"- {_esc(m)}" for m in rr.messages] + [""]
    else:
        lines += _verdict(rr.status, rr.messages, label="Workflow status")
        if vr is None:
            lines += ["**No verification was performed in this run: nothing has been verified.**", ""]
        else:
            lines += _verdict(vr.status, vr.reasons, label="Final verification status")
    iv = rr.initial_verification
    if iv is not None and vr is not None and iv.run_id != vr.run_id:
        lines += [f"Initial verification status: {_code(iv.status.value)} → final: {_code(vr.status.value)}", ""]

    # --------------------------------------------------- final verification
    if vr is not None:
        lines += _scope(vr)
        lines += _spec_section(vr)
        lines += _environment(vr)
    if rr.provider is not None or rr.generation is not None:
        lines += _provider_section(rr)
    if vr is not None:
        vdir = _round_dir(rr, vr)
        where = f" (artifacts under {_code(vdir)})" if vdir else ""
        lines += _gates(vr, intro=f"Final verification {_code(vr.run_id)}{where}.")
        lines += _findings(vr)
        lines += _counterexamples(vr)
        lines += _coverage(vr)
    if iv is not None and vr is not None and iv.run_id != vr.run_id:
        lines += _initial_section(iv)
    if rr.repair_rounds or rr.command in ("repair", "run"):
        lines += _repair_history(rr)
    extra = [f"{MOCK_DISCLAIMER}: repair and generation behaviour shown here is not evidence about any LLM."] if mock else []
    lines += _limitations(vr, extra)
    rerun_argv = ["leanc", rr.command] + ([rr.spec_path] if rr.spec_path else [])
    if rr.provider is not None:
        rerun_argv += ["--provider", rr.provider.name]
    lines += _reproduce(vr, rerun=shlex.join(rerun_argv))
    if rr.artifacts:
        lines += ["## Artifacts", "", "Paths relative to the run directory.", ""]
        lines += _table(["artifact", "path"], ([_esc(k), _code(v)] for k, v in sorted(rr.artifacts.items())))
        lines.append("")
    return _finish(lines)
