"""Frama-C/WP deductive verification gate (V2).

This is the only gate that can make the controller report ``PROVED``, so it
is deliberately strict about what counts as success.  ``Outcome.PASS``
requires all of:

1. **an approved contract** - the target's prototype in a *protected* header
   carries an ACSL contract (``ensures`` / ``assigns`` clauses), and Frama-C
   demonstrably applied it (proof obligations for those clauses exist for the
   target).  No contract -> ``INCONCLUSIVE`` (there is nothing to prove);
2. **every goal proved** - WP runs on the snapshot's editable ``.c`` files
   with ``-wp-rte`` (runtime-error guards become proof obligations) and the
   spec's provers; every goal - functional, RTE, helper contracts, loop
   invariants / assigns / variants, call-site preconditions, lemmas - must be
   *Valid*.  Unknown / timeout / stepout goals are ``INCONCLUSIVE``
   (``FailureReport.kind == "unproved"``): an unproved goal is not a
   counterexample, the code may be right and only lack loop invariants (this
   is what the repair loop's *proof repair* acts on);
3. **non-vacuity** - WP smoke tests (always on) must find no inconsistent
   precondition / behaviour assumption, dead code or dead loop
   (``kind == "vacuity"`` -> ``INCONCLUSIVE``);
4. **a fresh, honest run** - the WP proof cache is disabled (``-wp-cache
   none``) and the session / output directories are per-run, so a verdict
   never comes from an earlier run (a cache hit in the output is ``ERROR``);
   the console totals must agree with the JSON report; no annotation may be
   dropped (``annot-error`` is fatal); no called function may lack both code
   and specification (WP would rely on generated clauses).

``FAIL`` (-> ``COUNTEREXAMPLE_FOUND``) is reserved for a goal a prover
reports as *Invalid* - rare, since SMT models of unproved WP goals are not
trusted as counterexamples.  Missing tools, crashes, unparsable output and
prover failures are ``ERROR``; the overall time limit is ``TIMEOUT``.

Termination is proved only when ``wp.require_termination`` is set (every
loop then needs a ``loop variant`` and recursion a ``decreases``); otherwise
``terminates`` / ``decreases`` goals are deselected and the result is
explicitly *partial* correctness.

Frama-C (an opam installation usually lives outside ``/usr``) runs through
:meth:`BackendContext.run`, sandboxed when bwrap is available: its prefix,
the Why3 / prover directories are bound read-only, ``HOME=/tmp`` and
``WHY3CONFIG`` points at a Why3 configuration generated for this run by
``why3 config detect``.  Raw outputs (JSON report, console log, Why3 config)
are kept in ``ctx.raw_dir``.
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
import shutil
import signal as _signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from leanc.backends.acsl_rules import TargetContract, acsl_violations, target_contracts
from leanc.backends.tools import tool_version
from leanc.core.context import BackendContext
from leanc.core.models import Diagnostic, FailureReport, Location, StageResult, ToolInvocation
from leanc.core.redact import redact
from leanc.core.runner import DEFAULT_PATH, Limits, ProcResult
from leanc.core.status import Outcome, outcome_to_status
from leanc.repair.policy import CSource

__all__ = [
    "INSTALL_HINT",
    "MIN_FRAMAC_MAJOR",
    "STAGE",
    "TOOL",
    "FramaC",
    "build_wp_argv",
    "find_framac",
    "framac_version_key",
    "goal_kind",
    "parse_console",
    "run_wp",
]

STAGE = "wp"
TOOL = "frama-c-wp"
INSTALL_HINT = (
    "install Frama-C with the WP plug-in and an SMT prover, e.g. `opam install frama-c alt-ergo` "
    "(then `why3 config detect`), and put frama-c on PATH or set LEANC_FRAMAC=/path/to/frama-c"
)
MACHDEP = "x86_64"
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
_MAX_LISTED = 3
#: ``-wp-prover`` values that are not automatic provers: replaying scripts or
#: interactive sessions could reuse proofs that were not produced by this run.
NON_AUTOMATIC_PROVERS = frozenset(
    {"none", "script", "tip", "coq", "isabelle", "proofgeneral", "why3ide", "native", "native:alt-ergo"}
)
#: spec.verification.checks with a Frama-C RTE equivalent (beyond the defaults).
RTE_EXTRA_FLAGS: dict[str, list[str]] = {
    "unsigned-overflow": ["-warn-unsigned-overflow"],
    "conversion": ["-warn-signed-downcast", "-warn-unsigned-downcast"],
}
#: RTE guard classes always enabled (Frama-C defaults plus -warn-invalid-pointer).
RTE_CLASSES = (
    "invalid memory accesses (\\valid / \\valid_read)",
    "division / modulo by zero",
    "signed integer overflow",
    "invalid shift amounts and left shifts of negative values",
    "pointer arithmetic leaving its object (-warn-invalid-pointer)",
    "pointer downcasts",
    "float-to-int overflow and non-finite floating-point values",
    "trap representations of _Bool",
)


# =========================================================================== discovery
@dataclass(frozen=True)
class FramaC:
    frama_c: str  # executable as found
    real: str  # realpath of the executable
    bin_dir: str  # directory of the real executable (opam switch bin/)
    prefix: str  # installation prefix bound into the sandbox
    why3: str | None


def _first_existing(cands: list[str]) -> str | None:
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


#: Oldest Frama-C release whose WP output (JSON report, smoke tests, terminates /
#: exits handling) this backend understands; the parsing is tested with 33.0.
MIN_FRAMAC_MAJOR = 27


def framac_version_key(version_line: str | None) -> tuple[int, int]:
    """``(major, minor)`` of a ``frama-c -version`` line such as
    ``33.0 (Arsenic)``; ``(-1, -1)`` when unknown."""
    m = re.match(r"^\s*(\d+)\.(\d+)", version_line or "")
    return (int(m.group(1)), int(m.group(2))) if m else (-1, -1)


def _candidates() -> list[str]:
    out: list[str] = []
    for d in (os.environ.get("PATH", "") + os.pathsep + DEFAULT_PATH).split(os.pathsep):
        if d:
            out.append(os.path.join(d, "frama-c"))
    out += sorted(glob.glob("/opt/opam/*/bin/frama-c"))
    seen: set[str] = set()
    uniq: list[str] = []
    for c in out:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            real = os.path.realpath(c)
            if real not in seen:
                seen.add(real)
                uniq.append(c)
    return uniq


def find_framac() -> FramaC | None:
    """Locate frama-c: ``LEANC_FRAMAC`` (an executable path or name) if set;
    otherwise every ``frama-c`` on PATH (parent + standard directories) and
    ``/opt/opam/*/bin/frama-c``, preferring the newest version (a distribution
    package can be much older than the opam switch Lean C is tested with)."""
    env = os.environ.get("LEANC_FRAMAC")
    exe: str | None
    if env:
        exe = env if os.path.isabs(env) and os.access(env, os.X_OK) else shutil.which(env)
        if not exe:
            return None
    else:
        cands = _candidates()
        if not cands:
            return None
        exe = max(cands, key=lambda c: framac_version_key(tool_version(os.path.realpath(c), "-version")))
    real = os.path.realpath(exe)
    bin_dir = os.path.dirname(real)
    prefix = os.path.dirname(bin_dir) if os.path.basename(bin_dir) == "bin" else bin_dir
    # only the why3 of the same installation: its config format matches the
    # Why3 library linked into this frama-c
    why3 = _first_existing([os.path.join(bin_dir, "why3")])
    return FramaC(frama_c=exe, real=real, bin_dir=bin_dir, prefix=prefix, why3=why3)


def _under_usr(p: str) -> bool:
    return p == "/usr" or p.startswith("/usr/")


def _tool_path(fc: FramaC) -> str:
    return f"{fc.bin_dir}{os.pathsep}{DEFAULT_PATH}"


def _prover_exe(fc: FramaC, prover: str) -> str | None:
    name = prover.split(":", 1)[0].lower()
    return shutil.which(name, path=_tool_path(fc))


def _mount_dirs(fc: FramaC, provers: list[str]) -> list[str]:
    """Read-only bind mounts the tools need inside the sandbox (``/usr`` is
    always visible)."""
    out: list[str] = []
    for d in [fc.prefix, os.path.dirname(os.path.realpath(fc.why3)) if fc.why3 else None]:
        if d and not _under_usr(d) and d not in out:
            out.append(d)
    for p in provers:
        exe = _prover_exe(fc, p)
        if exe:
            d = os.path.dirname(os.path.realpath(exe))
            if not _under_usr(d) and not any(d == m or d.startswith(m + "/") for m in out):
                out.append(d)
    return out


# =========================================================================== argv
_INCLUDE_DIR_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./+-]*$")
_DEFINE_RE = re.compile(r"^(?:-D)?[A-Za-z_]\w*(?:=[A-Za-z0-9_.+-]*)?$")


def _cpp_args(ctx: BackendContext) -> list[str]:
    """Preprocessor arguments (Frama-C hands ``-cpp-extra-args`` to a shell
    command line, so only plain include directories / defines are accepted)."""
    spec = ctx.spec
    args: list[str] = []
    for d in spec.sources.include_dirs:
        if not _INCLUDE_DIR_RE.match(d):
            raise ValueError(f"include directory {d!r} cannot be passed safely to Frama-C's preprocessor")
        args.append(f"-I{d}")
    for d in spec.compile.defines:
        if not _DEFINE_RE.match(d):
            raise ValueError(f"define {d!r} cannot be passed safely to Frama-C's preprocessor")
        args.append(d if d.startswith("-D") else f"-D{d}")
    return args


def rte_flags(ctx: BackendContext) -> tuple[list[str], list[str]]:
    """(Frama-C ``-warn-*`` flags, names of extra checks honoured)."""
    flags = ["-warn-invalid-pointer"]
    extra: list[str] = []
    v = ctx.spec.verification
    for check in v.checks if v is not None else []:
        for f in RTE_EXTRA_FLAGS.get(check, []):
            if f not in flags:
                flags.append(f)
                if check not in extra:
                    extra.append(check)
    return flags, extra


def prover_timeout(ctx: BackendContext) -> int:
    return max(1, int(math.ceil(ctx.timeout(ctx.spec.wp.prover_timeout_s))))


def prover_parallelism() -> int:
    """Parallel prover processes (smoke tests pass by *timing out*, so each
    costs a full prover timeout: parallelism keeps the gate fast)."""
    return max(2, min(8, os.cpu_count() or 2))


def build_wp_argv(
    ctx: BackendContext,
    fc: FramaC,
    *,
    report_json: Path,
    work_dir: Path,
    why3_conf: Path | None,
) -> list[str]:
    """argv of the Frama-C/WP run (``cwd`` must be ``ctx.snapshot_dir``)."""
    spec = ctx.spec
    wp = spec.wp
    cpp = _cpp_args(ctx)
    flags, _ = rte_flags(ctx)
    argv: list[str] = [
        fc.frama_c,
        "-machdep", MACHDEP,
        "-kernel-warn-key", "annot-error=abort",
        "-kernel-warn-key", "typing:implicit-function-declaration=abort",
    ]  # fmt: skip
    if cpp:
        argv.append("-cpp-extra-args=" + " ".join(cpp))
    argv += flags
    argv += [
        "-wp",
        "-wp-rte",
        "-wp-smoke-tests",
        "-wp-prover", ",".join(wp.provers),
        "-wp-timeout", str(prover_timeout(ctx)),
        "-wp-memlimit", str(max(128, min(1000, wp.memory_mb // 2))),
        "-wp-par", str(prover_parallelism()),
        "-wp-cache", "none",
        "-wp-session", str(work_dir / "session"),
        "-wp-out", str(work_dir / "out"),
        "-wp-report-json", str(report_json),
    ]  # fmt: skip
    if why3_conf is not None:
        argv += ["-wp-why3-config", str(why3_conf)]
    if not wp.require_termination:
        # partial correctness: termination goals are deselected (reported as such)
        argv.append("-wp-prop=-@terminates,-@decreases")
    argv += list(spec.sources.editable)
    return argv


# =========================================================================== console parsing
@dataclass
class ConsoleSummary:
    proved: int | None = None
    total: int | None = None
    categories: dict[str, int] = field(default_factory=dict)
    smoke: tuple[int, int] | None = None
    cached: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    no_goals: bool = False


_PROVED_RE = re.compile(r"^\[wp\] Proved goals:\s*(\d+)\s*/\s*(\d+)\s*$", re.M)
_CATEGORY_RE = re.compile(r"^\s+([A-Za-z][\w .:-]*?)\s*:\s*(\d+)(?:\s*/\s*(\d+))?(?:\s*\(.*\))?\s*$")
_MSG_RE = re.compile(r"^\[([\w:.-]+)\]\s?(.*)$")
_ERROR_MARKERS = ("User Error", "Frama-C aborted", "aborted:", "Fatal error", "Unexpected error", "fatal error",
                  "Internal error")  # fmt: skip


def parse_console(text: str) -> ConsoleSummary:
    """Totals, warnings and errors from Frama-C's console output."""
    out = ConsoleSummary()
    lines = text.splitlines()
    m = None
    for m in _PROVED_RE.finditer(text):
        pass
    if m is not None:
        out.proved, out.total = int(m.group(1)), int(m.group(2))
        # category lines follow the summary line
        start = text[: m.end()].count("\n") + 1
        for line in lines[start:]:
            cm = _CATEGORY_RE.match(line)
            if not cm:
                break
            name = " ".join(cm.group(1).split())
            if name == "Smoke Tests" and cm.group(3) is not None:
                out.smoke = (int(cm.group(2)), int(cm.group(3)))
            else:
                out.categories[name] = int(cm.group(2))
    # messages (with their indented continuation lines)
    k = 0
    while k < len(lines):
        line = lines[k]
        mm = _MSG_RE.match(line)
        if not mm:
            k += 1
            continue
        block = [line]
        k += 1
        while k < len(lines) and lines[k].startswith("  ") and not _MSG_RE.match(lines[k]):
            block.append(lines[k])
            k += 1
        msg = " ".join(" ".join(block).split())
        if "[Cache]" in msg and re.search(r"\bfound\s*:\s*[1-9]", msg):
            out.cached.append(msg)
        if "Warning:" in msg:
            out.warnings.append(msg)
        if any(marker in msg for marker in _ERROR_MARKERS):
            out.errors.append(msg)
        if "No goal generated" in msg:
            out.no_goals = True
    return out


_MISSING_SPEC_RE = re.compile(r"Neither code nor specification for function (\w+)")
_GENERATED_CLAUSES_RE = re.compile(r"Neither code nor explicit [\w ,]+? for function (\w+)")
_MEM_HYP_RE = re.compile(r"Memory model hypotheses for function '(\w+)':\s*(.*)$")


# =========================================================================== goal classification
def _goal_rest(goal: dict) -> str:
    """Property name without function and behaviour prefixes, e.g.
    ``ensures_2``, ``assert_rte_mem_access``, ``loop_invariant_preserved``."""
    name = str(goal.get("goal") or "")
    prop = str(goal.get("property") or name)
    func = str(goal.get("function") or "")
    if func and f"_{func}_" in name:
        # goal names are <model>_<function>_<rest>; the "property" of a call-site
        # goal names the callee's clause instead, so prefer the goal name
        prop = name.split(f"_{func}_", 1)[1]
    elif func and prop.startswith(func + "_"):
        prop = prop[len(func) + 1 :]
    if "wp_smoke_" in prop:
        prop = prop[prop.index("wp_smoke_") :]
    bhv = goal.get("behavior")
    if bhv and prop.startswith(f"{bhv}_"):
        prop = prop[len(bhv) + 1 :]
    return prop


def goal_kind(goal: dict) -> str:
    """Human-readable class of a WP goal."""
    rest = _goal_rest(goal)
    if rest.startswith("wp_smoke_"):
        return "smoke test"
    if rest.startswith("assert_rte_"):
        kind = re.sub(r"_\d+$", "", rest[len("assert_rte_") :]).replace("_", " ")
        return f"runtime-error guard ({kind})"
    if rest.startswith("loop_invariant") or rest.startswith("check_loop_invariant"):
        phase = " (established)" if rest.endswith("established") else " (preserved)" if rest.endswith("preserved") else ""
        return "loop invariant" + phase
    if rest.startswith("loop_assigns"):
        return "loop assigns clause"
    if rest.startswith("loop_variant"):
        return "loop variant"
    m = re.match(r"call_(\w+?)_(?:\w+_)?requires", rest)
    if rest.startswith("call_") and m:
        return f"precondition of {m.group(1)}() at a call site"
    for prefix, what in (
        ("ensures", "post-condition"),
        ("assigns", "assigns clause (frame condition)"),
        ("exits", "exits clause"),
        ("terminates", "termination"),
        ("decreases", "decreases clause"),
        ("complete_", "completeness of behaviors"),
        ("disjoint_", "disjointness of behaviors"),
        ("assert", "assertion"),
        ("check", "check"),
    ):
        if rest.startswith(prefix):
            return what
    if str(goal.get("goal", "")).startswith(("typed_lemma", "lemma")) or rest.startswith("lemma"):
        return "lemma"
    if "requires" in rest:
        return "precondition"
    return "proof obligation"


# =========================================================================== backend
class _Lines:
    """Cached source lines of snapshot files (for stable descriptions)."""

    def __init__(self, root: Path):
        self.root = root
        self._cache: dict[str, list[str]] = {}

    def get(self, rel: str | None, line: int | None) -> str:
        if not rel or not line or os.path.isabs(rel) or rel.startswith(".."):
            return ""
        if rel not in self._cache:
            p = self.root / rel
            try:
                self._cache[rel] = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                self._cache[rel] = []
        lines = self._cache[rel]
        if not 1 <= line <= len(lines):
            return ""
        text = " ".join(lines[line - 1].split())
        text = re.sub(r"\s*\*/$", "", re.sub(r"^(?:/\*@|//@|@)\s*", "", text)) or text
        return text if len(text) <= 160 else text[:157] + "..."


def _relfile(path: str | None, snap_real: str, share: str | None) -> str | None:
    if not path:
        return None
    real = os.path.realpath(path) if os.path.isabs(path) else os.path.realpath(os.path.join(snap_real, path))
    if real == snap_real or real.startswith(snap_real + os.sep):
        return os.path.relpath(real, snap_real)
    if share and real.startswith(share.rstrip("/") + "/"):
        return "FRAMAC_SHARE/" + real[len(share.rstrip("/")) + 1 :]
    return path


def _signal_of(proc: ProcResult) -> int | None:
    sig = proc.signal
    if sig is None and proc.exit_code is not None and 128 < proc.exit_code <= 128 + 64:
        sig = proc.exit_code - 128  # bwrap reports a child killed by N as 128+N
    return sig


def _signal_name(sig: int) -> str:
    try:
        return _signal.Signals(sig).name
    except ValueError:
        return f"signal {sig}"


def _excerpt(items: list[str], limit: int = _MAX_LISTED) -> str:
    more = f" (+{len(items) - limit} more)" if len(items) > limit else ""
    return "; ".join(i[:300] for i in items[:limit]) + more


def _why3_provers(conf_text: str) -> list[dict]:
    """``[partial_prover]`` / ``[prover]`` sections of a Why3 config."""
    out: list[dict] = []
    for sec in re.split(r"(?m)^\[", conf_text):
        if not (sec.startswith("partial_prover]") or sec.startswith("prover]")):
            continue
        entry = {k: v for k, v in re.findall(r'(?m)^(name|version|path|alternative)\s*=\s*"([^"]*)"', sec)}
        if entry.get("name"):
            out.append(entry)
    return out


def run_wp(ctx: BackendContext) -> StageResult:
    """Run the Frama-C/WP gate (see the module docstring for the verdict rules)."""
    t0 = time.monotonic()
    spec = ctx.spec
    wp = spec.wp
    target = spec.target.function
    snap = Path(os.path.realpath(ctx.snapshot_dir))
    raw_root = ctx.raw_dir / STAGE
    work = ctx.build_dir / STAGE
    details: dict[str, Any] = {
        "frama_c": None,
        "frama_c_version": None,
        "why3": None,
        "why3_version": None,
        "provers": list(wp.provers) if wp else [],
        "prover_versions": {},
        "provers_detected": [],
        "prover_timeout_s": None,
        "require_termination": bool(wp and wp.require_termination),
        "machdep": MACHDEP,
        "memory_model": "Typed (WP default)",
        "rte_checks": list(RTE_CLASSES),
        "sandbox": ctx.sandbox.kind,
        "argv": {},
        "raw": {},
        "contract": [],
        "goals_total": 0,
        "goals_proved": 0,
        "goals_by_verdict": {},
        "smoke_tests": None,
        "console": {},
        "goals": [],
        "warnings": [],
        "memory_model_hypotheses": [],
        "library_functions_with_generated_clauses": [],
        "missing_specification": [],
        "notes": [],
        "assumptions": [],
    }
    invocations: list[ToolInvocation] = []
    diagnostics: list[Diagnostic] = []
    failures: list[FailureReport] = []

    def result(outcome: Outcome, summary: str) -> StageResult:
        details["assumptions"] = _assumptions(ctx, details, outcome)
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

    if wp is None:  # pragma: no cover - the spec model fills in defaults when the gate is listed
        return result(Outcome.ERROR, "the spec has no 'wp' section")
    if spec.language.is_cpp:
        return result(Outcome.ERROR, "Frama-C/WP supports C only; the spec is C++")

    fc = find_framac()
    if fc is None:
        return result(
            Outcome.ERROR,
            f"frama-c executable not found ({INSTALL_HINT}); deductive verification was not performed",
        )
    details["frama_c"] = fc.frama_c
    details["frama_c_version"] = tool_version(fc.real, "-version")
    if framac_version_key(details["frama_c_version"])[0] < MIN_FRAMAC_MAJOR:
        return result(
            Outcome.ERROR,
            f"{fc.frama_c} reports version {details['frama_c_version'] or '(unknown)'}; Frama-C >= "
            f"{MIN_FRAMAC_MAJOR} is required ({INSTALL_HINT})",
        )
    details["why3"] = fc.why3
    if fc.why3:
        details["why3_version"] = tool_version(fc.why3, "--version")
    for p in wp.provers:
        if p.split(":", 1)[0].lower() in NON_AUTOMATIC_PROVERS or p.lower() in NON_AUTOMATIC_PROVERS:
            return result(
                Outcome.ERROR,
                f"wp.provers contains {p!r}, which is not an automatic prover (script replay / interactive "
                "provers could reuse proofs not produced by this run); use e.g. alt-ergo, z3, cvc5",
            )
        exe = _prover_exe(fc, p)
        if exe:
            details["prover_versions"][p] = tool_version(exe, "--version")

    # --------------------------------------------------------- approved contract
    headers: dict[str, str] = {}
    missing: list[str] = []
    for h in spec.sources.headers:
        hp = snap / h
        if hp.is_file():
            headers[h] = hp.read_text(encoding="utf-8", errors="replace")
        else:
            missing.append(h)
    missing += [p for p in spec.sources.editable if not (snap / p).is_file()]
    if missing:
        return result(Outcome.ERROR, f"cannot run Frama-C/WP: missing input file(s) in the snapshot: {', '.join(missing)}")
    contracts = [c for c in target_contracts(target, headers) if c.has_obligations]
    details["contract"] = [
        {"file": c.file, "line": c.line, "clauses": c.clauses, "behaviors": c.behaviors} for c in contracts
    ]
    if not contracts:
        return result(
            Outcome.INCONCLUSIVE,
            f"no approved ACSL contract: no prototype of {target}() in the protected headers "
            f"({', '.join(spec.sources.headers) or 'none'}) carries an ACSL contract with ensures/assigns clauses, "
            "so there is nothing to prove (the contract must be part of the approved spec)",
        )

    # --------------------------------------------------------- untrusted ACSL (defence in depth)
    bad: list[str] = []
    for rel in spec.sources.editable:
        text = (snap / rel).read_text(encoding="utf-8", errors="replace")
        src = CSource(text)
        for rule, msg, off in acsl_violations(spec, src, headers):
            bad.append(f"{rel}:{src.line_of(off)}: {rule}: {msg}")
    if bad:
        details["notes"].append("untrusted ACSL in editable sources: " + "; ".join(bad[:10]))
        return result(
            Outcome.INCONCLUSIVE,
            f"the editable sources contain ACSL that could add unproved assumptions ({_excerpt(bad)}); WP was not "
            "run because its verdict could not be trusted (the integrity gate reports these violations)",
        )

    # --------------------------------------------------------- run
    try:
        for d in (raw_root, work, work / "session", work / "out"):
            d.mkdir(parents=True, exist_ok=True)
        _cpp_args(ctx)  # validates include dirs / defines before anything runs
    except (OSError, ValueError) as exc:
        return result(Outcome.ERROR, f"cannot prepare the Frama-C run: {exc}")
    limits = Limits(
        timeout_s=ctx.timeout(wp.timeout_s), memory_mb=wp.memory_mb, max_output_bytes=MAX_OUTPUT_BYTES, max_file_mb=1024
    )
    details["prover_timeout_s"] = prover_timeout(ctx)
    flags, extra_checks = rte_flags(ctx)
    details["rte_checks"] = list(RTE_CLASSES) + [
        {"unsigned-overflow": "unsigned integer wrap-around (spec check unsigned-overflow)",
         "conversion": "out-of-range integer conversions (spec check conversion)"}[c] for c in extra_checks
    ]
    details["rte_flags"] = flags
    mounts = ctx.mounts(extra_ro=_mount_dirs(fc, list(wp.provers)))
    why3_conf = work / "why3.conf"
    env = {"PATH": _tool_path(fc), "WHY3CONFIG": str(why3_conf)}
    deadline = time.monotonic() + limits.timeout_s

    def run(what: str, argv: list[str], lim: Limits) -> ProcResult:
        proc = ctx.run(argv, limits=lim, cwd=snap, env_extra=env, mounts=mounts)
        log = raw_root / f"{what}.log"
        log.write_text(redact(proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else "")), encoding="utf-8")
        details["raw"][f"{what}_log"] = ctx.relrun(log)
        details["argv"][what] = list(argv)
        note = f"output truncated at {lim.max_output_bytes} bytes" if proc.output_truncated else None
        invocations.append(proc.to_invocation("frama-c" if what == "wp" else "why3", ctx.run_dir,
                                              ctx.log_name(STAGE, what), note=note))
        return proc

    conf_used: Path | None = None
    if fc.why3:
        cproc = run("why3-config", [fc.why3, "config", "detect"], Limits(timeout_s=min(60.0, limits.timeout_s)))
        if cproc.timed_out or cproc.error or cproc.exit_code != 0 or not why3_conf.is_file():
            reason = cproc.error or ("timed out" if cproc.timed_out else f"exit {cproc.exit_code}")
            return result(
                Outcome.ERROR,
                f"`why3 config detect` failed ({reason}): {redact((cproc.stderr or cproc.stdout).strip())[-300:]}",
            )
        conf_text = why3_conf.read_text(encoding="utf-8", errors="replace")
        (raw_root / "why3.conf").write_text(conf_text, encoding="utf-8")
        details["raw"]["why3_conf"] = ctx.relrun(raw_root / "why3.conf")
        details["provers_detected"] = _why3_provers(conf_text)
        conf_used = why3_conf
    else:
        details["notes"].append("why3 executable not found next to frama-c: WP detected the provers on PATH itself")

    report = raw_root / "wp-report.json"
    try:
        report.unlink(missing_ok=True)  # never read a report left behind by an earlier run
    except OSError as exc:
        return result(Outcome.ERROR, f"cannot remove a stale WP report {report}: {exc}")
    try:
        argv = build_wp_argv(ctx, fc, report_json=report, work_dir=work, why3_conf=conf_used)
    except ValueError as exc:
        return result(Outcome.ERROR, f"cannot build the Frama-C command line: {exc}")
    remaining = max(1.0, deadline - time.monotonic())
    proc = run("wp", argv, Limits(timeout_s=remaining, memory_mb=limits.memory_mb,
                                   max_output_bytes=limits.max_output_bytes, max_file_mb=limits.max_file_mb))
    console_text = proc.stdout + "\n" + proc.stderr
    if proc.error:
        return result(Outcome.ERROR, f"frama-c could not be started: {proc.error} ({INSTALL_HINT})")
    if proc.timed_out:
        return result(
            Outcome.TIMEOUT,
            f"Frama-C/WP timed out after {limits.timeout_s:g}s (wp.timeout_s={wp.timeout_s:g}, timeout scale "
            f"{ctx.options.timeout_scale:g}); no verdict",
        )
    if proc.output_truncated:
        return result(Outcome.ERROR, f"Frama-C output exceeded {limits.max_output_bytes} bytes and was truncated")
    con = parse_console(console_text)
    details["console"] = {"proved": con.proved, "total": con.total, "categories": con.categories,
                          "smoke": list(con.smoke) if con.smoke else None}
    details["warnings"] = con.warnings[:100]
    sig = _signal_of(proc)
    if sig is not None:
        oom = sig in (_signal.SIGKILL, _signal.SIGABRT, _signal.SIGSEGV)
        hint = f" (possibly out of memory: wp.memory_mb={wp.memory_mb})" if oom else ""
        return result(Outcome.ERROR, f"Frama-C was killed by {_signal_name(sig)}{hint}")
    if con.errors or proc.exit_code != 0:
        excerpt = _excerpt(con.errors) or redact(console_text.strip())[-400:]
        return result(
            Outcome.ERROR,
            f"Frama-C failed (exit code {proc.exit_code}): {excerpt} - the program or its ACSL annotations could "
            "not be parsed / type-checked, or a prover is not configured; no verdict",
        )
    if con.cached:
        return result(Outcome.ERROR, f"WP reported cached proof results ({_excerpt(con.cached)}) although the cache "
                      "is disabled; a verdict must come from this run")

    # --------------------------------------------------------- JSON report
    try:
        goals = json.loads(report.read_text(encoding="utf-8"))
    except FileNotFoundError:
        goals = None if not con.no_goals else []
        if goals is None:
            return result(Outcome.ERROR, "Frama-C/WP produced no JSON report (-wp-report-json); no verdict")
    except (OSError, ValueError) as exc:
        return result(Outcome.ERROR, f"cannot parse the WP JSON report: {exc}")
    details["raw"]["report_json"] = ctx.relrun(report) if report.is_file() else None
    if not isinstance(goals, list) or not all(isinstance(g, dict) and "goal" in g and "passed" in g for g in goals):
        return result(Outcome.ERROR, "the WP JSON report has an unexpected structure; no verdict")
    cached_json = [g["goal"] for g in goals if g.get("cached")]
    if cached_json:
        return result(Outcome.ERROR, f"WP reported cached results for {len(cached_json)} goal(s) "
                      f"({', '.join(cached_json[:_MAX_LISTED])}); a verdict must come from this run")
    if con.total is None:
        if con.no_goals or not goals:
            return result(Outcome.INCONCLUSIVE, "WP generated no proof obligation at all; nothing was verified")
        return result(Outcome.ERROR, "the Frama-C output has no 'Proved goals: N / M' summary; no verdict")

    share = None
    try:
        share_candidates = glob.glob(os.path.join(fc.prefix, "share", "frama-c", "share"))
        share = share_candidates[0] if share_candidates else os.path.join(fc.prefix, "share", "frama-c")
    except OSError:  # pragma: no cover
        share = None
    lines = _Lines(snap)
    snap_real = str(snap)
    by_verdict: dict[str, int] = {}
    smoke_total = smoke_failed = 0
    unproved: list[dict] = []
    invalid: list[dict] = []
    prover_failed: list[dict] = []
    smoke_bad: list[dict] = []
    target_goals: list[dict] = []
    for g in goals:
        verdict = str(g.get("verdict", "none")).lower()
        smoke = bool(g.get("smoke"))
        passed = bool(g.get("passed"))
        rel = _relfile(g.get("file"), snap_real, share)
        prov = [p for p in (g.get("provers") or []) if isinstance(p, dict)]
        winner = next((p.get("prover") for p in prov if p.get("success")), None)
        entry = {
            "goal": g.get("goal"),
            "property": g.get("property"),
            "kind": goal_kind(g),
            "function": g.get("function"),
            "behavior": g.get("behavior"),
            "file": rel,
            "line": g.get("line"),
            "smoke": smoke,
            "passed": passed,
            "verdict": verdict,
            "prover": winner or (prov[-1].get("prover") if prov else None),
        }
        details["goals"].append(entry)
        if not smoke:
            by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
        if g.get("function") == target and not smoke:
            target_goals.append(entry)
        if smoke:
            smoke_total += 1
            if not passed:
                smoke_failed += 1
                smoke_bad.append(entry)
            continue
        if passed and verdict == "valid":
            continue
        if verdict == "invalid":
            invalid.append(entry)
        elif verdict == "failed":
            prover_failed.append(entry)
        else:
            unproved.append(entry)
    nonsmoke = len(goals) - smoke_total
    details["goals_by_verdict"] = by_verdict
    details["smoke_tests"] = {"passed": smoke_total - smoke_failed, "total": smoke_total}
    extra_proved = con.categories.get("Terminating", 0) + con.categories.get("Unreachable", 0)
    details["goals_total"] = con.total
    details["goals_proved"] = con.proved
    if len(goals) + extra_proved != con.total:
        return result(
            Outcome.ERROR,
            f"inconsistent WP output: the JSON report lists {len(goals)} goal(s) (+{extra_proved} discharged without "
            f"a prover) but the console reports {con.total}; no verdict",
        )
    if con.smoke is not None and con.smoke != (smoke_total - smoke_failed, smoke_total):
        return result(Outcome.ERROR, f"inconsistent WP output: smoke tests {con.smoke[0]}/{con.smoke[1]} on the "
                      f"console vs {smoke_total - smoke_failed}/{smoke_total} in the JSON report; no verdict")
    if prover_failed:
        names = ", ".join(str(e["goal"]) for e in prover_failed[:_MAX_LISTED])
        return result(Outcome.ERROR, f"prover failure on {len(prover_failed)} goal(s) ({names}): the prover crashed "
                      f"or is not usable ({', '.join(wp.provers)}); no verdict")

    for w in con.warnings:
        mh = _MEM_HYP_RE.search(w)
        if mh and len(details["memory_model_hypotheses"]) < 20:
            details["memory_model_hypotheses"].append(f"{mh.group(1)}: {mh.group(2)[:300]}")
        ms = _MISSING_SPEC_RE.search(w)
        if ms and ms.group(1) not in details["missing_specification"]:
            details["missing_specification"].append(ms.group(1))
        gc = _GENERATED_CLAUSES_RE.search(w)
        if gc and gc.group(1) not in details["library_functions_with_generated_clauses"]:
            details["library_functions_with_generated_clauses"].append(gc.group(1))

    # --------------------------------------------------------- failure reports
    report_ref = details["raw"].get("report_json")
    tried = ", ".join(wp.provers)
    for e in invalid:
        loc = Location(file=e["file"], line=e["line"], function=e["function"])
        text = lines.get(e["file"], e["line"])
        failures.append(FailureReport(
            id=f"wp:{e['goal']}", kind="counterexample", tool=TOOL, property_id=e["goal"],
            description=f"{e['kind']} refuted: `{text}`" if text else f"{e['kind']} refuted",
            location=loc,
            explanation=f"a prover reported the goal {e['goal']} as INVALID: the code violates this property for "
            "some input allowed by the approved preconditions.",
            raw_ref=report_ref,
        ))
    for e in unproved:
        loc = Location(file=e["file"], line=e["line"], function=e["function"])
        text = lines.get(e["file"], e["line"])
        bhv = f" (behavior {e['behavior']})" if e.get("behavior") else ""
        failures.append(FailureReport(
            id=f"wp:{e['goal']}", kind="unproved", tool=TOOL, property_id=e["goal"],
            description=(f"{e['kind']}{bhv} not proved: `{text}`" if text else f"{e['kind']}{bhv} not proved"),
            location=loc,
            explanation=(
                f"Frama-C/WP could not prove {e['goal']} with {tried} ({details['prover_timeout_s']} s per goal; "
                f"verdict: {e['verdict']}). Unproved is not refuted: the code may be correct but need proof "
                "annotations (loop invariants covering every variable the loop changes, loop assigns, helper "
                "contracts), or it may really violate the property for some input allowed by the preconditions."
            ),
            raw_ref=report_ref,
        ))
    for e in smoke_bad:
        failures.append(_smoke_failure(e, lines, target, report_ref))
    for f in failures:
        diagnostics.append(Diagnostic(
            tool=TOOL, rule_id={"unproved": "unproved", "vacuity": "smoke-test", "counterexample": "invalid"}[f.kind],
            severity="error" if f.kind == "counterexample" else "warning",
            message=f"{f.description} [{f.property_id}]", location=f.location,
        ))

    # --------------------------------------------------------- contract really applied?
    applied = _contract_applied(contracts, target_goals)
    if not applied[0]:
        failures.append(FailureReport(
            id="wp:contract-not-applied", kind="unproved", tool=TOOL, property_id="contract-not-applied",
            description=f"the approved contract of {target}() produced no {applied[1]} proof obligation",
            location=Location(file=contracts[0].file, line=contracts[0].line, function=target),
            explanation="Frama-C did not attach the approved ACSL contract to the definition of the target (is the "
            "protected header included by the editable source that defines it?). Without it nothing about the "
            "specified behaviour is proved.",
            raw_ref=report_ref,
        ))

    # --------------------------------------------------------- verdict
    n_un, n_smoke = len(unproved), len(smoke_bad)
    proved_txt = f"{con.proved}/{con.total} goals proved"
    if invalid:
        return result(
            Outcome.FAIL,
            f"Frama-C/WP refuted {len(invalid)} goal(s): "
            + "; ".join(f.description for f in failures if f.kind == "counterexample")[:600]
            + (f"; plus {n_un} unproved goal(s)" if n_un else ""),
        )
    problems: list[str] = []
    if not applied[0]:
        problems.append(f"the approved contract was not applied to {target}() (no {applied[1]} goal for it)")
    if n_un:
        listed = "; ".join(f"{e['goal']} ({e['verdict']})" for e in unproved[:_MAX_LISTED])
        more = f" (+{n_un - _MAX_LISTED} more)" if n_un > _MAX_LISTED else ""
        problems.append(f"{n_un} goal(s) not proved: {listed}{more}")
    if n_smoke:
        listed = "; ".join(f.description for f in failures if f.kind == "vacuity")
        problems.append(f"{n_smoke} smoke test(s) failed - the contract or code is (partly) vacuous: {listed[:400]}")
    if details["missing_specification"]:
        problems.append(
            "called function(s) with neither code nor ACSL specification ("
            + ", ".join(details["missing_specification"])
            + "): WP would rely on clauses Frama-C generated, not on proved or approved facts"
        )
    if nonsmoke == 0 or not target_goals:
        problems.append(f"WP generated no proof obligation for {target}() (is it defined in the editable sources?)")
    if smoke_total == 0:
        problems.append("WP ran no smoke test, so vacuity cannot be excluded")
    if problems:
        return result(Outcome.INCONCLUSIVE, f"Frama-C/WP: {proved_txt}; " + "; ".join(problems))
    if con.proved != con.total:
        return result(Outcome.ERROR, f"inconsistent WP output: every goal of the JSON report is valid but the "
                      f"console reports {proved_txt}; no verdict")
    term = "termination proved" if wp.require_termination else "partial correctness (termination not required)"
    return result(
        Outcome.PASS,
        f"Frama-C/WP: all {con.total} goals proved ({nonsmoke - len(unproved)} proof obligations incl. RTE guards, "
        f"{smoke_total} smoke tests passed) for the approved contract of {target}(); {term}; provers {tried}",
    )


def _contract_applied(contracts: list[TargetContract], target_goals: list[dict]) -> tuple[bool, str]:
    """The approved contract has ensures/assigns clauses: the target must have
    the corresponding goals, otherwise Frama-C never saw the contract."""
    rests = [str(e.get("property") or "") for e in target_goals]
    want_ensures = any(c.clauses.get("ensures") for c in contracts)
    want_assigns = any(c.clauses.get("assigns") for c in contracts)
    if want_ensures and not any(re.search(r"(^|_)ensures(_|$)", r) for r in rests):
        return False, "ensures"
    if want_assigns and not any(re.search(r"(^|_)assigns(_|$)", r) for r in rests):
        return False, "assigns"
    return True, ""


def _smoke_failure(e: dict, lines: _Lines, target: str, raw_ref: str | None) -> FailureReport:
    rest = _goal_rest({k: e.get(k) for k in ("goal", "property", "function", "behavior")})
    func = e.get("function") or target
    text = lines.get(e["file"], e["line"])
    shown = f": `{text}`" if text else ""
    if rest.startswith("wp_smoke_default_requires"):
        desc = f"inconsistent precondition: the requires clauses of {func}() can never hold together"
    elif rest.startswith("wp_smoke_") and rest.endswith("_assumes") or (e.get("behavior") and "assumes" in rest):
        desc = f"behavior {e.get('behavior') or '?'} of {func}() can never apply (its assumes contradicts the requires)"
    elif "dead_code" in rest:
        desc = f"dead code in {func}(): unreachable under the preconditions{shown}"
    elif "dead_loop" in rest:
        desc = f"inconsistent loop invariant in {func}(): the loop body is unreachable{shown}"
    elif "dead_call" in rest:
        desc = f"a call in {func}() never returns according to the callee's contract{shown}"
    else:
        desc = f"smoke test {rest} of {func}() failed{shown}"
    return FailureReport(
        id=f"wp:smoke:{e['goal']}",
        kind="vacuity",
        tool=TOOL,
        property_id=e["goal"],
        description=desc,
        location=Location(file=e["file"], line=e["line"], function=func),
        explanation=(
            "WP smoke tests try to prove 'false' from the hypotheses at a program point; here a prover succeeded, "
            "so every property proved there holds vacuously. An inconsistent requires / assumes is a defect of the "
            "approved contract (fixing it needs a re-approved spec); dead code or an inconsistent loop invariant is "
            "a defect of the editable code or its annotations."
        ),
        raw_ref=raw_ref,
    )


def _assumptions(ctx: BackendContext, details: dict, outcome: Outcome) -> list[str]:
    """Honest scope statements of a WP verdict."""
    spec = ctx.spec
    wp = spec.wp
    target = spec.target.function
    if outcome is not Outcome.PASS:
        return [f"Frama-C/WP did not establish the approved contract of {target}() (outcome {outcome.value}): "
                "no deductive guarantee is claimed"]
    contracts = ", ".join(f"{c['file']}:{c['line']}" for c in details.get("contract", [])) or "?"
    provers = ", ".join(f"{p} {details['prover_versions'].get(p) or ''}".strip() for p in wp.provers)
    out = [
        f"deductive proof with Frama-C {details.get('frama_c_version') or '?'} / WP (Why3 "
        f"{(details.get('why3_version') or '?').replace('Why3 platform, version ', '')}; provers: {provers}; "
        f"{details.get('prover_timeout_s')} s per goal) of the approved ACSL contract of {target}() ({contracts}) "
        "for ALL inputs satisfying its preconditions - unbounded, no input-size or loop bound",
        "absence of runtime errors for all such inputs, as encoded by Frama-C's RTE guards (-wp-rte): "
        + ", ".join(details.get("rte_checks") or RTE_CLASSES)
        + "; NOT checked: reads of uninitialised memory, pointer alignment (\\aligned), function-pointer validity",
        (
            "total correctness: termination was required and proved (terminates / loop variants / decreases)"
            if wp.require_termination
            else "partial correctness only: termination is NOT proved (wp.require_termination is false); a "
            "non-terminating execution satisfies the contract vacuously"
        ),
        "under WP's Typed memory model (memory is accessed through its declared types; casts between "
        "incompatible pointer types are not modelled) and Frama-C's machine model "
        f"{details.get('machdep')} (32-bit int, 64-bit long and pointers, little-endian)",
        "library calls are justified by Frama-C's libc ACSL specifications, which are assumed, not proved"
        + (
            " (default exits/terminates clauses generated for: "
            + ", ".join(details["library_functions_with_generated_clauses"])
            + ")"
            if details.get("library_functions_with_generated_clauses")
            else ""
        ),
        "loop invariants, loop variants, assertions and helper-function contracts written in the editable sources "
        "are proof obligations (all proved), not assumptions",
        f"non-vacuity (best effort): all {(details.get('smoke_tests') or {}).get('total', 0)} WP smoke tests passed - "
        "the provers found no inconsistent precondition, behaviour assumption, dead code or dead loop",
        "the result concerns the C source as preprocessed and parsed by Frama-C; compiler correctness and the "
        "platform's actual libc are outside its scope",
    ]
    for h in details.get("memory_model_hypotheses") or []:
        out.append(f"WP memory-model hypothesis (assumed): {h}")
    return out
