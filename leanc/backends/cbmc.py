"""CBMC bounded-model-checking gate.

This is the only *formal* gate of Lean C V1, so it is deliberately strict
about what counts as success.  ``Outcome.PASS`` (which the controller turns
into ``VERIFIED_BOUNDED``) requires all of:

1. **verify run** - CBMC with the spec's (mandatory + extra) property checks,
   ``--unwind N --unwinding-assertions``: every real property holds *and*
   every unwinding assertion holds (all loops fully unwound within N);
2. **vacuity run** - the harness is re-checked with ``-DLEANC_VACUITY_CHECK``
   so every ``LEANC_REACH("label")`` probe becomes ``assert(0)``; CBMC must
   report each probe as FAILED (= reachable).  A probe that "holds" means the
   assumptions (or the code) make that point unreachable and every check
   after it is vacuous.  A disabled check or a harness without probes is
   *inconclusive*, never success;
3. no tool trouble: a timeout is ``TIMEOUT``; a missing executable, a crash,
   running out of memory, truncated / unparsable JSON or CBMC front-end
   errors (``PARSING ERROR`` / ``CONVERSION ERROR``) are ``ERROR``.

Real counterexamples (``Outcome.FAIL`` -> ``COUNTEREXAMPLE_FOUND``) dominate:
a counterexample is a concrete execution, valid even if the unwind bound is
insufficient elsewhere.  Insufficient unwinding alone is ``INCONCLUSIVE``
(the result is not sound for the input space; raising the bound changes the
spec and needs re-approval).

The optional **coverage run** (``--cover location``) is informational only.
CBMC 5.x rejects ``--cover`` together with ``--unwinding-assertions``, so the
coverage run uses the same bounds without unwinding assertions.

CBMC runs through :meth:`BackendContext.run` (sandboxed when available) with
``cwd = snapshot`` and relative source paths, so locations in its output are
workspace-relative.  Raw JSON of every run is saved under ``ctx.raw_dir``.
"""

from __future__ import annotations

import json
import os
import re
import signal as _signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from leanc.backends.counterexample import (
    REACH_PREFIX,
    failure_from_cbmc_property,
    is_reach_probe,
    is_unwinding_property,
    location_from_cbmc,
    property_class,
    unwind_failure,
    vacuity_failure,
)
from leanc.foundation.models import (
    Diagnostic,
    FailureReport,
    Location,
    StageResult,
    ToolInvocation,
)
from leanc.foundation.runner import Limits, ProcResult
from leanc.foundation.status import Outcome, outcome_to_status
from leanc.toolchain.context import BackendContext

STAGE = "cbmc"
TOOL = "cbmc"

#: Spec check name (``leanc.specs.model.CheckName``) -> CBMC flag.
CHECK_FLAGS: dict[str, str] = {
    "bounds": "--bounds-check",
    "pointer": "--pointer-check",
    "div-by-zero": "--div-by-zero-check",
    "signed-overflow": "--signed-overflow-check",
    "unsigned-overflow": "--unsigned-overflow-check",
    "pointer-overflow": "--pointer-overflow-check",
    "conversion": "--conversion-check",
    "undefined-shift": "--undefined-shift-check",
    "float-overflow": "--float-overflow-check",
    "nan": "--nan-check",
    "memory-leak": "--memory-leak-check",
    "pointer-primitive": "--pointer-primitive-check",
    "enum-range": "--enum-range-check",
}

VACUITY_DEFINE = "-DLEANC_VACUITY_CHECK"
#: Per-stream output cap for CBMC (traces of many failures can be large).
MAX_OUTPUT_BYTES = 64 * 1024 * 1024

Mode = Literal["verify", "vacuity", "coverage"]
MODES: tuple[str, ...] = ("verify", "vacuity", "coverage")

_OOM_MARKERS = ("out of memory", "bad_alloc", "cannot allocate memory", "memory exhausted")
_FRONTEND_MARKERS = ("PARSING ERROR", "CONVERSION ERROR")
_MAX_LISTED = 3

# --------------------------------------------------------------------------- argv


def _define_args(defines: list[str]) -> tuple[list[str], list[str]]:
    """``-D`` arguments for CBMC from ``compile.defines``.

    ``NDEBUG`` is *not* forwarded: it would turn the trusted harness's
    ``assert()`` calls into no-ops and make the verification silently weaker.
    Dropped defines are returned so the caller can record them.
    """
    args: list[str] = []
    dropped: list[str] = []
    for d in defines:
        arg = d if d.startswith("-D") else f"-D{d}"
        name = arg[2:].split("=", 1)[0].strip()
        if name == "NDEBUG":
            dropped.append(d)
            continue
        args.append(arg)
    return args, dropped


def build_cbmc_argv(ctx: BackendContext, *, mode: Mode = "verify") -> list[str]:
    """argv of one CBMC run (``cwd`` must be ``ctx.snapshot_dir``).

    ``verify``: property checks of ``spec.verification.effective_checks()``;
    ``vacuity``: ``-DLEANC_VACUITY_CHECK`` and no extra checks (only probe
    reachability matters); ``coverage``: ``--cover location`` (without
    ``--unwinding-assertions``, which CBMC does not accept together with it).
    """
    if mode not in MODES:
        raise ValueError(f"unknown CBMC mode {mode!r} (expected one of {', '.join(MODES)})")
    cbmc = ctx.tools.cbmc
    if not cbmc:
        raise ValueError("cbmc is not available (ctx.tools.cbmc is None)")
    spec = ctx.spec
    v = spec.verification
    defines, _ = _define_args(spec.compile.defines)
    argv: list[str] = [
        cbmc,
        *spec.sources.editable,
        v.harness,
        *ctx.include_args(),
        f"-I{os.path.abspath(ctx.include_dir)}",
        *defines,
    ]
    if mode == "vacuity":
        argv.append(VACUITY_DEFINE)
    elif mode == "verify":
        for check in v.effective_checks():
            flag = CHECK_FLAGS.get(check)
            if flag is None:  # pragma: no cover - CheckName and CHECK_FLAGS are kept in sync
                raise ValueError(f"no CBMC flag known for check {check!r}")
            argv.append(flag)
    else:
        argv += ["--cover", "location"]
    argv += ["--unwind", str(v.unwind)]
    if v.unwindset:
        argv += ["--unwindset", ",".join(f"{loop}:{bound}" for loop, bound in sorted(v.unwindset.items()))]
    if mode != "coverage":
        argv.append("--unwinding-assertions")
    if v.malloc_may_fail:
        argv += ["--malloc-may-fail", "--malloc-fail-null"]
    if v.object_bits:
        argv += ["--object-bits", str(v.object_bits)]
    argv.append("--json-ui")
    return argv


# --------------------------------------------------------------------------- output parsing


def _salvage_messages(text: str) -> list[dict]:
    """Decode as many complete top-level message objects as possible from a
    truncated / malformed ``--json-ui`` array (e.g. CBMC aborted mid-output),
    so that error messages it printed are not lost."""
    dec = json.JSONDecoder()
    i = text.find("[")
    if i < 0:
        return []
    i += 1
    out: list[dict] = []
    n = len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n or text[i] == "]":
            break
        try:
            obj, i = dec.raw_decode(text, i)
        except ValueError:
            break
        if isinstance(obj, dict):
            out.append(obj)
    return out


def parse_cbmc_json(text: str) -> tuple[list[dict], str | None]:
    """Parse CBMC ``--json-ui`` output.  Returns ``(messages, error)``;
    ``error`` is None for well-formed output, otherwise a description, and
    ``messages`` then holds whatever could be salvaged."""
    s = (text or "").strip()
    if not s:
        return [], "empty output"
    try:
        data = json.loads(s)
    except json.JSONDecodeError as exc:
        return _salvage_messages(s), f"invalid JSON ({exc.msg} at line {exc.lineno} column {exc.colno})"
    if isinstance(data, dict):
        return [data], "unexpected JSON: top level is an object, not an array"
    if not isinstance(data, list):
        return [], f"unexpected JSON: top level is {type(data).__name__}"
    return [m for m in data if isinstance(m, dict)], None


@dataclass
class CbmcOutput:
    """The parts of a CBMC ``--json-ui`` message stream Lean C uses."""

    messages: list[dict] = field(default_factory=list)
    parse_error: str | None = None
    results: list[dict] | None = None
    goals: list[dict] | None = None
    cprover_status: str | None = None
    program: str | None = None
    platform: str | None = None  # e.g. "64-bit x86_64 linux"
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)

    def error_texts(self) -> list[str]:
        return [str(m.get("messageText", "")).strip() for m in self.errors if str(m.get("messageText", "")).strip()]


_VERSION_LINE_RE = re.compile(r"^CBMC version (?P<ver>\S+(?: \([^)]*\))?)\s+(?P<plat>.+)$")


def interpret_output(text: str) -> CbmcOutput:
    messages, err = parse_cbmc_json(text)
    out = CbmcOutput(messages=messages, parse_error=err)
    for m in messages:
        if "result" in m and isinstance(m["result"], list):
            out.results = [r for r in m["result"] if isinstance(r, dict)]
        elif "goals" in m and isinstance(m["goals"], list):
            out.goals = [g for g in m["goals"] if isinstance(g, dict)]
        elif "cProverStatus" in m:
            out.cprover_status = str(m["cProverStatus"])
        elif "program" in m:
            out.program = str(m["program"])
        elif "messageText" in m:
            mtype = m.get("messageType")
            if mtype == "ERROR":
                out.errors.append(m)
            elif mtype == "WARNING":
                out.warnings.append(m)
            elif out.platform is None:
                mm = _VERSION_LINE_RE.match(str(m["messageText"]).strip())
                if mm:
                    out.platform = mm.group("plat")
    return out


def _same_path(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return os.path.normpath(a).removeprefix("./") == os.path.normpath(b).removeprefix("./")


def _program_version(program: str | None) -> str | None:
    if not program:
        return None
    return program[5:].strip() if program.startswith("CBMC ") else program


# --------------------------------------------------------------------------- execution


@dataclass
class _Run:
    mode: str
    argv: list[str]
    proc: ProcResult
    raw_path: Path
    raw_ref: str
    out: CbmcOutput
    invocation: ToolInvocation


@dataclass
class _Problem:
    outcome: Outcome
    summary: str
    diagnostics: list[Diagnostic] = field(default_factory=list)


def _execute(ctx: BackendContext, mode: Mode, limits: Limits) -> _Run:
    argv = build_cbmc_argv(ctx, mode=mode)
    # An absolute cwd: the sandbox changes into it by path (``--chdir``).
    proc = ctx.run(argv, limits=limits, cwd=Path(os.path.abspath(ctx.snapshot_dir)))
    raw_path = ctx.raw_dir / f"cbmc-{mode}.json"
    raw_path.write_text(proc.stdout, encoding="utf-8")
    note = None
    if proc.output_truncated:
        note = f"output truncated at {limits.max_output_bytes} bytes"
    inv = proc.to_invocation(TOOL, ctx.run_dir, ctx.log_name(STAGE, mode), note=note)
    return _Run(
        mode=mode,
        argv=list(argv),
        proc=proc,
        raw_path=raw_path,
        raw_ref=ctx.relrun(raw_path),
        out=interpret_output(proc.stdout),
        invocation=inv,
    )


def _signal_name(sig: int) -> str:
    try:
        return _signal.Signals(sig).name
    except ValueError:
        return f"signal {sig}"


def _error_diagnostics(out: CbmcOutput, rule: str) -> list[Diagnostic]:
    diags = []
    for m in out.errors:
        text = str(m.get("messageText", "")).strip()
        if not text or text in _FRONTEND_MARKERS:
            continue
        diags.append(
            Diagnostic(
                tool=TOOL,
                rule_id=rule,
                severity="error",
                message=text,
                location=location_from_cbmc(m.get("sourceLocation")),
            )
        )
    return diags


def _messages_excerpt(out: CbmcOutput, stderr: str, limit: int = _MAX_LISTED) -> str:
    items = []
    for m in out.errors:
        text = str(m.get("messageText", "")).strip()
        if not text or text in _FRONTEND_MARKERS:
            continue
        loc = location_from_cbmc(m.get("sourceLocation"))
        items.append(f"{loc.short()}: {text}" if loc and loc.file else text)
    if not items and stderr.strip():
        items = [ln.strip() for ln in stderr.strip().splitlines()[-limit:] if ln.strip()]
    more = f" (+{len(items) - limit} more)" if len(items) > limit else ""
    return "; ".join(items[:limit]) + more


def _is_oom(run: _Run) -> bool:
    texts = " ".join([*run.out.error_texts(), run.proc.stderr]).lower()
    return any(k in texts for k in _OOM_MARKERS)


def _check_run(run: _Run, limits: Limits, ctx: BackendContext, *, expect: str) -> _Problem | None:
    """Tool-level problems of one run (``expect`` = "result" or "goals")."""
    proc, out, mode = run.proc, run.out, run.mode
    v = ctx.spec.verification
    what = f"CBMC ({mode} run)"
    if proc.error:
        return _Problem(Outcome.ERROR, f"{what} could not be started: {proc.error}")
    if proc.timed_out:
        return _Problem(
            Outcome.TIMEOUT,
            f"{what} timed out after {limits.timeout_s:g}s (verification.timeout_s={v.timeout_s:g}, "
            f"timeout scale {ctx.options.timeout_scale:g}); no verdict",
        )
    if proc.output_truncated:
        return _Problem(
            Outcome.ERROR,
            f"{what} output exceeded {limits.max_output_bytes} bytes and was truncated; "
            "the result cannot be interpreted",
        )
    oom = _is_oom(run)
    sig = proc.signal
    if sig is None and proc.exit_code is not None and 128 < proc.exit_code <= 128 + 64:
        # bwrap (and shells) report a child killed by signal N as exit status 128+N.
        sig = proc.exit_code - 128
    if sig is not None:
        hint = ""
        if oom or sig in (_signal.SIGKILL, _signal.SIGABRT, _signal.SIGSEGV):
            hint = f" (possibly out of memory: verification.memory_mb={v.memory_mb})"
        return _Problem(
            Outcome.ERROR,
            f"{what} was killed by {_signal_name(sig)}{hint}",
            _error_diagnostics(out, "cbmc-error"),
        )
    data = out.results if expect == "result" else out.goals
    if oom and (out.parse_error or data is None):
        # With results present, properties CBMC could not decide are reported
        # as such by the caller (counterexamples found before running out of
        # memory are still genuine).
        return _Problem(
            Outcome.ERROR,
            f"{what} ran out of memory (verification.memory_mb={v.memory_mb}): {_messages_excerpt(out, proc.stderr)}",
            _error_diagnostics(out, "out-of-memory"),
        )
    if out.parse_error:
        excerpt = _messages_excerpt(out, proc.stderr)
        return _Problem(
            Outcome.ERROR,
            f"{what} produced unparsable JSON output ({out.parse_error}; exit code {proc.exit_code})"
            + (f": {excerpt}" if excerpt else ""),
            _error_diagnostics(out, "cbmc-error"),
        )
    if data is None:
        texts_upper = " ".join(out.error_texts())
        if "PARSING ERROR" in texts_upper:
            kind, rule = "could not parse the program (PARSING ERROR)", "parsing-error"
        elif "CONVERSION ERROR" in texts_upper:
            kind, rule = "could not type-check / convert the program (CONVERSION ERROR)", "conversion-error"
        else:
            kind, rule = f"produced no {expect} (exit code {proc.exit_code})", "cbmc-error"
        excerpt = _messages_excerpt(out, proc.stderr)
        return _Problem(
            Outcome.ERROR,
            f"{what} {kind}" + (f": {excerpt}" if excerpt else ""),
            _error_diagnostics(out, rule),
        )
    return None


# --------------------------------------------------------------------------- harness probes


def strip_c_comments(code: str) -> str:
    """Replace C comments with spaces (string / char literals are kept), so
    that commented-out probes are not mistaken for real ones."""
    out: list[str] = []
    i, n = 0, len(code)
    while i < n:
        c = code[i]
        nxt = code[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = code.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif c == "/" and nxt == "*":
            j = code.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in code[i:j]))
            i = j
        elif c in "\"'":
            j = i + 1
            while j < n and code[j] != c and code[j] != "\n":
                j += 2 if code[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(code[i:j])
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


_REACH_USE_RE = re.compile(r"\bLEANC_REACH\s*\(")
_REACH_LABEL_RE = re.compile(r'\bLEANC_REACH\s*\(\s*"((?:[^"\\\n]|\\.)*)"\s*\)')


def find_probes(harness_text: str) -> tuple[bool, list[str]]:
    """``(has_probes, labels)`` of the ``LEANC_REACH("label")`` probes in a
    harness (comments ignored; labels in order of first appearance)."""
    code = strip_c_comments(harness_text)
    if not _REACH_USE_RE.search(code):
        return False, []
    labels = list(dict.fromkeys(m.group(1) for m in _REACH_LABEL_RE.finditer(code)))
    return True, labels


def probe_occurrences(harness_text: str) -> tuple[int, dict[str, int]]:
    """``(occurrences, per_label_counts)``: how many ``LEANC_REACH(`` uses the
    comment-stripped harness contains, and how often each literal label is
    used.  Every occurrence must show up as (at least) one probe property in
    the vacuity run; a missing one is treated as unreachable."""
    code = strip_c_comments(harness_text)
    counts: dict[str, int] = {}
    for m in _REACH_LABEL_RE.finditer(code):
        counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return len(_REACH_USE_RE.findall(code)), counts


def _blank_string_literals(code: str) -> str:
    """Replace the contents of string / char literals with spaces (comments
    must already be stripped) so text inside literals is never matched."""
    out: list[str] = []
    i, n = 0, len(code)
    while i < n:
        c = code[i]
        if c in "\"'":
            j = i + 1
            while j < n and code[j] != c and code[j] != "\n":
                j += 2 if code[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(c + " " * max(0, j - i - 2) + (code[j - 1] if j - i >= 2 else ""))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


_CONDITIONAL_RE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b")
_DIRECTIVE_RE = re.compile(r"^\s*#")


def _blank_preprocessor_regions(code: str) -> str:
    """Blank preprocessor directive lines (including ``#define`` bodies with
    line continuations) and everything inside conditional blocks: whether an
    ``#if`` branch is compiled cannot be decided without preprocessing, so
    those lines are conservatively ignored when counting assertions."""
    out: list[str] = []
    depth = 0
    continued = False
    for line in code.split("\n"):
        if continued:
            continued = line.rstrip().endswith("\\")
            out.append("")
            continue
        m = _CONDITIONAL_RE.match(line)
        if m:
            kind = m.group(1)
            if kind in ("if", "ifdef", "ifndef"):
                depth += 1
            elif kind == "endif":
                depth = max(0, depth - 1)
            continued = line.rstrip().endswith("\\")
            out.append("")
            continue
        if _DIRECTIVE_RE.match(line):
            continued = line.rstrip().endswith("\\")
            out.append("")
            continue
        out.append("" if depth > 0 else line)
    return "\n".join(out)


_MAIN_DEF_RE = re.compile(r"\bmain\s*\([^;{})]*\)\s*\{")
_ASSERT_USE_RE = re.compile(r"\b(?:__CPROVER_assert|assert)\s*\(")


def _function_body(code: str, start: int) -> str:
    """Text from the ``{`` at ``start`` to its matching ``}`` (literals must
    already be blanked)."""
    depth = 0
    for i in range(start, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[start : i + 1]
    return code[start:]


def count_harness_assertions(harness_text: str) -> int:
    """Number of ``assert(...)`` / ``__CPROVER_assert(...)`` calls written
    directly in the harness's ``main`` body.

    CBMC lists every such call as an ``assertion`` property of ``main`` (even
    in dead code or with a constant condition).  Deliberately conservative,
    so a legitimate harness never trips the check: assertions in comments,
    string literals, ``#define`` bodies, preprocessor conditional blocks and
    helper functions (which CBMC drops when they are never called) are not
    counted; macro-expanded assertions only add properties.
    """
    code = _blank_preprocessor_regions(_blank_string_literals(strip_c_comments(harness_text)))
    m = _MAIN_DEF_RE.search(code)
    if not m:
        return 0
    body = _function_body(code, m.end() - 1)
    return len(_ASSERT_USE_RE.findall(body))


# --------------------------------------------------------------------------- coverage


def _parse_lines(spec: Any) -> set[int]:
    out: set[int] = set()
    for part in str(spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a.strip().isdigit() and b.strip().isdigit():
                lo, hi = int(a), int(b)
                if 0 <= hi - lo <= 100000:
                    out.update(range(lo, hi + 1))
        elif part.isdigit():
            out.add(int(part))
    return out


def coverage_by_file(goals: list[dict], files: list[str]) -> dict[str, dict]:
    """Per-file location coverage from CBMC ``--cover location`` goals:
    ``{file: {"goals": n, "covered": k, "uncovered_lines": [...]}}``.  A line
    is uncovered when it belongs only to unsatisfied basic blocks."""
    stats: dict[str, dict] = {}
    cov_lines: dict[str, set[int]] = {}
    unc_lines: dict[str, set[int]] = {}
    for f in files:
        stats[f] = {"goals": 0, "covered": 0, "uncovered_lines": []}
        cov_lines[f], unc_lines[f] = set(), set()
    for g in goals:
        loc = location_from_cbmc(g.get("sourceLocation"))
        file = loc.file if loc else None
        if file not in stats:
            continue
        satisfied = str(g.get("status", "")).lower() == "satisfied"
        lines: set[int] = set()
        bbl = g.get("basicBlockLines")
        if isinstance(bbl, dict):
            per_file = bbl.get(file)
            if isinstance(per_file, dict):
                for spec in per_file.values():
                    lines |= _parse_lines(spec)
        if not lines and loc is not None and loc.line is not None:
            lines.add(loc.line)
        stats[file]["goals"] += 1
        if satisfied:
            stats[file]["covered"] += 1
            cov_lines[file] |= lines
        else:
            unc_lines[file] |= lines
    for f in files:
        stats[f]["uncovered_lines"] = sorted(unc_lines[f] - cov_lines[f])
    return stats


# --------------------------------------------------------------------------- assumptions


def _bound_text(b: Any) -> str:
    lo, hi = b.min, b.max
    if lo is not None and hi is not None:
        rng = f"{lo} <= {b.name} <= {hi}"
    elif lo is not None:
        rng = f"{b.name} >= {lo}"
    elif hi is not None:
        rng = f"{b.name} <= {hi}"
    else:
        return f"{b.name}: {b.note}" if b.note else f"{b.name}: no explicit bound"
    return rng + (f" ({b.note})" if b.note else "")


def scope_assumptions(
    ctx: BackendContext,
    *,
    version: str | None,
    platform: str | None = None,
    vacuity: dict | None = None,
    no_body: list[str] | None = None,
    dropped_defines: list[str] | None = None,
) -> list[str]:
    """Human-readable statements of exactly what a CBMC result covers."""
    v = ctx.spec.verification
    out = []
    bound = f"bounded model checking with CBMC {version or '(unknown version)'}: every loop / recursion unwound at most {v.unwind} time(s)"
    if v.unwindset:
        bound += " (per-loop bounds: " + ", ".join(f"{k}={b}" for k, b in sorted(v.unwindset.items())) + ")"
    bound += "; unwinding assertions are enabled, so a loop needing more iterations makes the result inconclusive"
    out.append(bound)
    out.append(
        "checked property classes: "
        + ", ".join(v.effective_checks())
        + " - plus every assertion of the approved harness; other properties are not checked"
    )
    out.append(
        f"inputs as constrained by the approved harness {v.harness} (its LEANC_ASSUME preconditions) "
        f"calling {ctx.spec.target.function}(); executions outside these constraints are not covered"
    )
    for b in v.input_bounds:
        out.append("input bound (documented in the spec, enforced by the harness): " + _bound_text(b))
    if v.malloc_may_fail:
        out.append("malloc model: allocations may fail and then return NULL (--malloc-may-fail --malloc-fail-null)")
    else:
        out.append("malloc model: allocations never fail (CBMC default); out-of-memory paths are not explored")
    out.append(
        f"pointer model: {v.object_bits} object bits" if v.object_bits else "pointer model: CBMC default object bits"
    )
    if platform:
        out.append(f"C semantics as modelled by CBMC for {platform}")
    if vacuity is not None and vacuity.get("status") == "ok":
        n = len(vacuity.get("properties") or vacuity.get("probes") or {})
        out.append(f"non-vacuity: all {n} LEANC_REACH probe(s) of the harness are reachable")
    if no_body:
        out.append(
            "functions without a body are treated by CBMC as returning arbitrary values: " + ", ".join(sorted(no_body))
        )
    if dropped_defines:
        out.append(
            "compile define(s) not passed to CBMC because they would disable harness assertions: "
            + ", ".join(dropped_defines)
        )
    return out


# --------------------------------------------------------------------------- vacuity probes


@dataclass
class _ProbeOutcome:
    details: dict
    failures: list[FailureReport] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    inconclusive: str = ""
    problem: _Problem | None = None


def _evaluate_probes(run: _Run, harness_text: str, *, harness: str, unwind_insufficient: bool) -> _ProbeOutcome:
    """Judge the vacuity run.

    Probes are keyed by CBMC *property id*, not by label: two probes may
    share a label, and each must be reachable on its own (one reachable
    ``"branch"`` must not hide an unreachable one).  The harness must also
    yield at least as many probe properties as it contains ``LEANC_REACH(``
    occurrences; occurrences CBMC did not report (code compiled out, in a
    never-called function, ...) count as unreachable.
    """
    occurrences, source_counts = probe_occurrences(harness_text)
    props: list[tuple[str, str, str, dict]] = []  # (property id, label, status, result)
    for k, r in enumerate(run.out.results or []):
        if not is_reach_probe(r):
            continue
        pid = str(r.get("property") or f"<probe {k}>")
        props.append((pid, str(r.get("description"))[len(REACH_PREFIX) :], str(r.get("status", "")).upper(), r))

    reported: dict[str, int] = {}
    for _, label, _, _ in props:
        reported[label] = reported.get(label, 0) + 1
    duplicates = {lb: n for lb, n in reported.items() if n > 1}

    def where(r: dict) -> str:
        loc = location_from_cbmc(r.get("sourceLocation"))
        return f"{loc.file}:{loc.line}" if loc and loc.file and loc.line is not None else "?"

    def display(label: str, r: dict) -> str:
        return f"{label} ({where(r)})" if label in duplicates else label

    properties = {
        pid: {"label": label, "reachable": status == "FAILURE", "status": status, "location": where(r)}
        for pid, label, status, r in props
    }
    probes: dict[str, bool] = {}
    for _, label, status, _ in props:
        probes[label] = probes.get(label, True) and status == "FAILURE"
    for label in source_counts:
        probes.setdefault(label, False)  # in the source, never reported
    det: dict[str, Any] = {"status": "ok", "probes": probes, "properties": properties, "occurrences": occurrences}
    out = _ProbeOutcome(details=det)
    if duplicates:
        det["duplicate_labels"] = duplicates
        out.notes.append(
            "duplicate LEANC_REACH label(s): "
            + ", ".join(f"{lb!r} x{n}" for lb, n in sorted(duplicates.items()))
            + "; every probe occurrence is checked separately"
        )

    undecided = [display(label, r) for _, label, status, r in props if status not in ("FAILURE", "SUCCESS")]
    if undecided:
        det["status"] = "skipped"
        det["note"] = "undecided probes"
        out.problem = _Problem(
            Outcome.ERROR,
            f"CBMC (vacuity run) could not decide the reachability of probe(s) {', '.join(undecided)}"
            + (f": {_messages_excerpt(run.out, run.proc.stderr)}" if run.out.errors else ""),
        )
        return out

    seen_ids: set[str] = set()

    def add(f: FailureReport) -> None:
        base, n = f.id, 2
        while f.id in seen_ids:
            f.id = f"{base}#{n}"
            n += 1
        seen_ids.add(f.id)
        out.failures.append(f)

    unreachable: list[str] = []
    for pid, label, status, r in props:
        if status != "SUCCESS":
            continue
        vf = vacuity_failure(label, result=r, raw_ref=run.raw_ref, unwind_insufficient=unwind_insufficient)
        if label in duplicates:
            vf.id = f"cbmc:vacuity:{label}@{where(r)}"
        add(vf)
        unreachable.append(display(label, r))

    missing_by_label = {lb: n - reported.get(lb, 0) for lb, n in source_counts.items() if n > reported.get(lb, 0)}
    for label, missing in missing_by_label.items():
        vf = vacuity_failure(label, raw_ref=run.raw_ref, reported=False, unwind_insufficient=unwind_insufficient)
        if reported.get(label, 0):
            vf.id = f"cbmc:vacuity:{label}@unreported"
            vf.description = (
                f'{missing} of {source_counts[label]} occurrence(s) of LEANC_REACH("{label}") were not reported '
                "by CBMC (the code containing them is not reachable from the harness entry point)"
            )
        add(vf)
    missing_total = max(0, occurrences - len(props))
    unattributed = missing_total - sum(missing_by_label.values())
    if unattributed > 0:
        add(
            FailureReport(
                id="cbmc:vacuity:unreported-probes",
                kind="vacuity",
                tool=TOOL,
                description=(
                    f"{unattributed} LEANC_REACH occurrence(s) of the harness {harness} were not reported by CBMC "
                    "(compiled out, or in code not reachable from the harness entry point)"
                ),
                location=Location(file=harness),
                explanation=(
                    "Each LEANC_REACH occurrence must appear as a probe in CBMC's vacuity run; missing probes are "
                    "treated as unreachable, so the verification cannot be shown to be non-vacuous."
                ),
                raw_ref=run.raw_ref,
            )
        )
    det["unreported_occurrences"] = max(missing_total, sum(missing_by_label.values()))

    if not props and not out.failures:
        det["status"] = "unchecked"
        out.inconclusive = (
            "the harness mentions LEANC_REACH but CBMC reported no probe in the vacuity run; vacuity cannot be checked"
        )
        return out
    if out.failures:
        det["status"] = "vacuous"
        parts = []
        if unreachable:
            parts.append(f"probe(s) {', '.join(unreachable)} unreachable")
        n_missing = det["unreported_occurrences"]
        if n_missing:
            names = ", ".join(missing_by_label) if missing_by_label else "with non-literal labels"
            parts.append(f"{n_missing} LEANC_REACH occurrence(s) not reported by CBMC ({names})")
        out.inconclusive = (
            "vacuous harness: "
            + "; ".join(parts)
            + " - the assumptions/code make that point unreachable, so the checks there are vacuous"
        )
    return out


# --------------------------------------------------------------------------- the gate

_NO_BODY_RE = re.compile(r"no body for (?:function|callee) '?([A-Za-z_][\w$]*)'?")


def _summarize_failure(f: FailureReport) -> str:
    where = f" at {f.location.short()}" if f.location else ""
    s = f"{f.property_id} ({f.description}){where}"
    params = [i for i in f.inputs if i.name.startswith("param ")] or f.inputs
    if params:
        s += " with " + ", ".join(f"{i.name.removeprefix('param ')}={i.value}" for i in params[:6])
    return s


def run_cbmc(ctx: BackendContext) -> StageResult:
    """Run the CBMC gate (see the module docstring for the verdict rules)."""
    t0 = time.monotonic()
    spec = ctx.spec
    v = spec.verification
    target = spec.target.function
    _, dropped_defines = _define_args(spec.compile.defines)
    details: dict[str, Any] = {
        "cbmc": ctx.tools.cbmc,
        "cbmc_version": ctx.tools.cbmc_version,
        "sandbox": ctx.sandbox.kind,
        "argv": {},
        "raw": {},
        "checks": v.effective_checks(),
        "unwind": v.unwind,
        "unwindset": dict(v.unwindset),
        "malloc_may_fail": v.malloc_may_fail,
        "object_bits": v.object_bits,
        "properties_total": 0,
        "properties_passed": 0,
        "properties_failed": 0,
        "properties_unresolved": 0,
        "unwind_failures": [],
        "vacuity": {"status": "skipped", "probes": {}},
        "coverage": None,
        "cbmc_warnings": [],
        "notes": [],
        "assumptions": [],
    }
    invocations: list[ToolInvocation] = []
    diagnostics: list[Diagnostic] = []
    failures: list[FailureReport] = []
    if dropped_defines:
        details["notes"].append(
            f"compile.defines {dropped_defines} not passed to CBMC: NDEBUG would disable the harness's assert() checks"
        )
    meta: dict[str, Any] = {"platform": None, "no_body": []}

    def result(outcome: Outcome, summary: str) -> StageResult:
        details["assumptions"] = scope_assumptions(
            ctx,
            version=details.get("cbmc_version"),
            platform=meta["platform"],
            vacuity=details.get("vacuity"),
            no_body=meta["no_body"],
            dropped_defines=dropped_defines,
        )
        return StageResult(
            stage=STAGE,
            outcome=outcome,
            status=outcome_to_status(STAGE, outcome),
            summary=summary,
            diagnostics=diagnostics,
            failures=failures,
            invocations=invocations,
            details=details,
            duration_s=round(time.monotonic() - t0, 3),
        )

    if not ctx.tools.cbmc:
        return result(
            Outcome.ERROR,
            "cbmc executable not found (install CBMC, e.g. `apt-get install cbmc`, or set LEANC_CBMC); "
            "formal verification was not performed",
        )
    missing = [p for p in [*spec.sources.editable, v.harness] if not (ctx.snapshot_dir / p).is_file()]
    if missing:
        return result(Outcome.ERROR, f"cannot run CBMC: missing input file(s) in the snapshot: {', '.join(missing)}")
    harness_text = (ctx.snapshot_dir / v.harness).read_text(encoding="utf-8", errors="replace")

    limits = Limits(timeout_s=ctx.timeout(v.timeout_s), memory_mb=v.memory_mb, max_output_bytes=MAX_OUTPUT_BYTES)

    def execute(mode: Mode) -> _Run:
        run = _execute(ctx, mode, limits)
        invocations.append(run.invocation)
        details["argv"][mode] = run.argv
        details["raw"][mode] = run.raw_ref
        if not details.get("cbmc_version"):
            details["cbmc_version"] = _program_version(run.out.program)
        meta["platform"] = meta["platform"] or run.out.platform
        return run

    # ------------------------------------------------------------ verify run
    vrun = execute("verify")
    problem = _check_run(vrun, limits, ctx, expect="result")
    if problem:
        diagnostics.extend(problem.diagnostics)
        return result(problem.outcome, problem.summary)

    for w in vrun.out.warnings:
        text = str(w.get("messageText", "")).strip()
        if len(details["cbmc_warnings"]) < 50:
            details["cbmc_warnings"].append(text)
        m = _NO_BODY_RE.search(text)
        if m and m.group(1) not in meta["no_body"]:
            meta["no_body"].append(m.group(1))
            diagnostics.append(
                Diagnostic(
                    tool=TOOL,
                    rule_id="no-body",
                    severity="warning",
                    message=f"no body for function {m.group(1)}: CBMC treats calls to it as returning arbitrary values",
                    location=location_from_cbmc(w.get("sourceLocation")),
                )
            )

    results = vrun.out.results or []

    # Second line of defence against neutralised harness assertions (e.g.
    # editable code defining __assert_fail, which makes CBMC drop every
    # assert() of the harness from its property list): each assertion written
    # in the harness's main() must appear as an "assertion" property of main.
    expected_asserts = count_harness_assertions(harness_text)
    listed_asserts = 0
    for r in results:
        loc = location_from_cbmc(r.get("sourceLocation"))
        if (
            loc is not None
            and loc.function == "main"
            and _same_path(loc.file, v.harness)
            and property_class(r.get("property"), r.get("description")) == "assertion"
        ):
            listed_asserts += 1
    details["harness_assertions"] = {"in_source": expected_asserts, "cbmc_properties": listed_asserts}
    if listed_asserts < expected_asserts:
        diagnostics.append(
            Diagnostic(
                tool=TOOL,
                rule_id="missing-assertions",
                severity="error",
                message=(
                    f"the harness main() contains {expected_asserts} assertion(s) but CBMC lists only "
                    f"{listed_asserts} assertion property(ies) for it"
                ),
                location=Location(file=v.harness, function="main"),
            )
        )
        return result(
            Outcome.ERROR,
            "harness assertions missing from CBMC's property list (assert machinery overridden?): "
            f"{v.harness} main() contains {expected_asserts} assertion(s), CBMC checked {listed_asserts}; "
            "no verdict is possible",
        )

    real_fail: list[dict] = []
    unwind_fail: list[dict] = []
    unresolved: list[dict] = []
    real_total = passed = 0
    for r in results:
        status = str(r.get("status", "")).upper()
        if is_unwinding_property(r):
            if status == "FAILURE":
                unwind_fail.append(r)
            elif status != "SUCCESS":
                unresolved.append(r)
            continue
        real_total += 1
        if status == "FAILURE":
            real_fail.append(r)
        elif status == "SUCCESS":
            passed += 1
        else:
            unresolved.append(r)
    details["properties_total"] = real_total
    details["properties_passed"] = passed
    details["properties_failed"] = len(real_fail)
    details["properties_unresolved"] = len(unresolved)
    details["unwinding_assertions_total"] = sum(1 for r in results if is_unwinding_property(r))
    details["cprover_status"] = vrun.out.cprover_status
    if vrun.proc.exit_code not in (0, 10):
        details["notes"].append(f"unexpected CBMC exit code {vrun.proc.exit_code} for the verify run")

    if target in meta["no_body"]:
        details["notes"].append(
            f"{len(real_fail)} reported violation(s) discarded: they depend on arbitrary return values of the undefined {target}()"
        )
        return result(
            Outcome.INCONCLUSIVE,
            f"the function under verification {target}() has no definition in the editable sources; "
            "CBMC treats it as returning arbitrary values, so no verdict is possible",
        )

    for r in real_fail:
        f = failure_from_cbmc_property(r, raw_ref=vrun.raw_ref, harness=v.harness, target_function=target)
        failures.append(f)
        diagnostics.append(
            Diagnostic(
                tool=TOOL,
                rule_id=property_class(f.property_id, f.description),
                severity="error",
                message=f"{f.description} [{f.property_id}]",
                location=f.location,
            )
        )
    unwind_reports = []
    for r in unwind_fail:
        f = unwind_failure(r, raw_ref=vrun.raw_ref, unwind=v.unwind, harness=v.harness, target_function=target)
        unwind_reports.append(f)
        details["unwind_failures"].append(
            {"property": f.property_id, "description": f.description, "location": f.location.short() if f.location else None}
        )
        diagnostics.append(
            Diagnostic(
                tool=TOOL,
                rule_id="unwind",
                severity="warning",
                message=f"{f.description} [{f.property_id}]: unwind bound {v.unwind} is insufficient",
                location=f.location,
            )
        )
    failures.extend(unwind_reports)

    unwind_loops = ", ".join(
        f"{f.property_id}" + (f" at {f.location.short()}" if f.location else "") for f in unwind_reports[:_MAX_LISTED]
    )
    unwind_text = (
        f"{len(unwind_reports)} unwinding assertion(s) failed ({unwind_loops}): the unwind bound {v.unwind} is "
        "insufficient, so executions needing more iterations were not explored and the result is not sound "
        "for the input space; raising the bound requires a re-approved spec"
        if unwind_reports
        else ""
    )

    if real_fail:
        details["vacuity"] = {"status": "skipped", "probes": {}, "note": "not run: real property violations found"}
        real = failures[: len(real_fail)]
        summary = f"CBMC found {len(real)} property violation(s) (counterexamples): " + "; ".join(
            _summarize_failure(f) for f in real[:_MAX_LISTED]
        )
        if len(real) > _MAX_LISTED:
            summary += f"; and {len(real) - _MAX_LISTED} more"
        if unwind_text:
            summary += f". In addition, {unwind_text}"
        if unresolved:
            summary += f". {len(unresolved)} property(ies) could not be decided"
            if _is_oom(vrun):
                summary += f" (CBMC ran out of memory: verification.memory_mb={v.memory_mb})"
        return result(Outcome.FAIL, summary)

    if unresolved:
        details["vacuity"] = {"status": "skipped", "probes": {}, "note": "not run: undecided properties"}
        names = ", ".join(f"{r.get('property')} ({r.get('status')})" for r in unresolved[:_MAX_LISTED])
        excerpt = _messages_excerpt(vrun.out, vrun.proc.stderr)
        oom = _is_oom(vrun)
        diagnostics.extend(_error_diagnostics(vrun.out, "out-of-memory" if oom else "cbmc-error"))
        head = f"CBMC ran out of memory (verification.memory_mb={v.memory_mb}) and " if oom else "CBMC "
        return result(
            Outcome.ERROR,
            f"{head}could not decide {len(unresolved)} property(ies): {names}" + (f"; {excerpt}" if excerpt else ""),
        )

    # ------------------------------------------------------------ vacuity
    vac_problem: _Problem | None = None
    inconclusive: list[str] = [unwind_text] if unwind_text else []
    if not v.vacuity_check:
        details["vacuity"] = {"status": "disabled", "probes": {}}
        inconclusive.append("vacuity check disabled in spec; cannot exclude vacuous verification")
    else:
        has_probes, _ = find_probes(harness_text)
        if not has_probes:
            details["vacuity"] = {"status": "unchecked", "probes": {}}
            inconclusive.append(
                f'no LEANC_REACH probes in the harness {v.harness}; vacuity cannot be checked '
                "(add LEANC_REACH(\"label\") after the call under verification)"
            )
        else:
            arun = execute("vacuity")
            vac_problem = _check_run(arun, limits, ctx, expect="result")
            if vac_problem is not None:
                details["vacuity"] = {"status": "skipped", "probes": {}, "note": f"vacuity run failed: {vac_problem.summary}"}
                diagnostics.extend(vac_problem.diagnostics)
            else:
                outcome_v = _evaluate_probes(
                    arun,
                    harness_text,
                    harness=v.harness,
                    unwind_insufficient=bool(unwind_reports),
                )
                details["vacuity"] = outcome_v.details
                details["notes"].extend(outcome_v.notes)
                failures.extend(outcome_v.failures)
                for vf in outcome_v.failures:
                    diagnostics.append(
                        Diagnostic(tool=TOOL, rule_id="vacuity", severity="warning", message=vf.description, location=vf.location)
                    )
                if outcome_v.problem is not None:
                    vac_problem = outcome_v.problem
                elif outcome_v.inconclusive:
                    inconclusive.append(
                        outcome_v.inconclusive
                        + (
                            " (possibly only because executions beyond the unwind bound were cut off)"
                            if unwind_reports
                            else ""
                        )
                    )

    if vac_problem is not None:
        summary = vac_problem.summary
        if inconclusive:
            summary += ". Also: " + "; ".join(inconclusive)
        return result(vac_problem.outcome, summary)
    if real_total == 0 and not inconclusive:
        inconclusive.append("CBMC generated no property to check for this harness and code; nothing was verified")
    if inconclusive:
        return result(Outcome.INCONCLUSIVE, "CBMC verdict inconclusive: " + "; ".join(inconclusive))

    # ------------------------------------------------------------ coverage (informational)
    if v.coverage:
        crun = execute("coverage")
        cproblem = _check_run(crun, limits, ctx, expect="goals")
        if cproblem is not None:
            details["coverage_error"] = cproblem.summary
            details["notes"].append(f"coverage run failed (non-blocking): {cproblem.summary}")
        else:
            cov = coverage_by_file(crun.out.goals or [], list(spec.sources.editable))
            details["coverage"] = cov
            for file, st in cov.items():
                unc = st["uncovered_lines"]
                details["notes"].append(
                    f"coverage (non-blocking) {file}: {st['covered']}/{st['goals']} basic block(s) reachable by the harness"
                    + (f"; never reached: lines {', '.join(map(str, unc))}" if unc else "")
                )

    n_probes = len(details["vacuity"].get("properties", {}))
    return result(
        Outcome.PASS,
        f"CBMC: all {real_total} properties hold for the approved harness within unwind {v.unwind} "
        f"(all loops fully unwound; all {n_probes} vacuity probe(s) reachable; checks: {', '.join(v.effective_checks())})",
    )
