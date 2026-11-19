"""Clang compile gate: build every editable source to an object file and turn
compiler output into structured :class:`~leanc.core.models.Diagnostic` objects.

Why a separate compile gate?  Every later gate (static analysis, sanitizer
tests, CBMC) needs code that at least type-checks.  Running a plain ``-c``
compile first gives the fastest, most precise feedback (file / line / column /
warning flag) both to humans and to the repair agent, and lets the controller
stop early on ``COMPILE_ERROR``.

Design notes
------------
* The compiler runs with ``cwd = snapshot_dir`` and *relative* source paths, so
  clang prints workspace-relative file names; absolute names that still point
  into the snapshot (e.g. in linker output built with ``-g``) are relativised.
* Machine-friendly output is forced with ``-fno-color-diagnostics
  -fno-caret-diagnostics -fdiagnostics-show-option`` (one line per diagnostic,
  the ``[-Wflag]`` that triggered it at the end).
* ``note:`` lines are *attached* to the preceding diagnostic as
  :class:`~leanc.core.models.PathStep` entries (and their text is appended to
  that diagnostic's ``raw``); they are **not** emitted as separate diagnostics,
  so counts of errors / warnings are not inflated.  A note with no preceding
  diagnostic (rare; e.g. a driver note) is kept as a standalone
  ``severity="note"`` diagnostic rather than dropped.
* A crash of the compiler itself is a tool error (``Outcome.ERROR``), never a
  compile error attributed to the code under test.
"""

from __future__ import annotations

import os
import posixpath
import re
import time
from pathlib import Path

from leanc.core.context import BackendContext
from leanc.core.models import Diagnostic, FailureReport, Location, PathStep, StageResult, TraceStep
from leanc.core.redact import redact
from leanc.core.runner import Limits
from leanc.core.status import Outcome, outcome_to_status

STAGE = "compile"

#: Flags appended after the spec's own flags (so they win) to make clang's
#: output stable and parseable.
DIAGNOSTIC_FLAGS: tuple[str, ...] = (
    "-fno-color-diagnostics",
    "-fno-caret-diagnostics",
    "-fdiagnostics-show-option",
)

# --------------------------------------------------------------------------- parsing

_SEV = r"fatal error|error|warning|note|remark"
# file:line[:col]: severity: message
_DIAG_RE = re.compile(rf"^(?P<file>.+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<sev>{_SEV}):\s?(?P<msg>.*)$")
# prog-or-file: severity: message   (no line number)
_NOLINE_RE = re.compile(rf"^(?P<file>[^\s:][^:]*?):\s*(?P<sev>{_SEV}):\s?(?P<msg>.*)$")
# severity: message   (cc1 / driver messages without any prefix)
_BARE_RE = re.compile(rf"^(?P<sev>{_SEV}):\s?(?P<msg>.*)$")
# "[-Wflag]" / "[-Werror,-Wflag]" / "[core.NullDereference]" at the end of a message
_OPT_RE = re.compile(r"^(?P<msg>.*?)\s*\[(?P<opt>[^\[\]]+)\]\s*$")
_CHECKER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")
_SUMMARY_RE = re.compile(r"^\d+ (?:warning|error)s?(?: and \d+ (?:warning|error)s?)? generated\.?\s*$")
_INCLUDED_RE = re.compile(r"^In file included from .+:\d+:")
# Further levels of a nested include stack ("                 from b.h:2:").
_INCLUDED_FROM_RE = re.compile(r"^\s+from .+:\d+[:,]\s*$")

# Linker output (GNU ld / bfd / gold, lld, gcc's collect2).
_LINKER_PROG = r"(?:\S*/)?(?:ld|ld\.[A-Za-z0-9_]+|lld|collect2)"
_LD_CONTEXT_RE = re.compile(
    rf"^(?:{_LINKER_PROG}: )?(?P<obj>.+?): in function [`'‘](?P<func>[^'`’]+)['’]:\s*$"
)
_LD_SYMREF_RE = re.compile(
    rf"^(?:{_LINKER_PROG}: )?(?P<where>.+?): (?P<msg>(?:undefined reference to|multiple definition of) .*)$"
)
_LD_WHERE_RE = re.compile(r"^(?P<file>.+?)(?::(?P<line>\d+))?:\(")
_LD_GENERIC_RE = re.compile(
    rf"^(?P<prog>{_LINKER_PROG}): (?:(?P<sev>fatal error|error|warning): )?(?P<msg>.*)$"
)
_LLD_REF_RE = re.compile(r"^>>>\s*(?P<msg>.*)$")

# Program names that can prefix a "name: error: ..." line *without* being a file.
_DRIVER_RE = re.compile(
    r"^(?:.*/)?(?:clang|clang\+\+|clang-cl|clang-\d+(?:\.\d+)*|cc|c\+\+|cc1|gcc|g\+\+|gcc-\d+|"
    r"collect2|ld|ld\.[A-Za-z0-9_]+|lld|lld-link|ar|llvm-[A-Za-z0-9_-]+)$"
)
_SOURCE_EXT_RE = re.compile(r"\.(?:c|h|i|cc|cpp|cxx|hpp|hh|inc|s|S)$")

_CRASH_MARKERS = (
    "PLEASE submit a bug report",
    "frontend command failed due to signal",
    "unable to execute command",
    "Stack dump:",
    "clang frontend command failed with exit code",
)


def _rel_path(path: str, base_dir: Path | None) -> str:
    """Return ``path`` relative to ``base_dir`` when it lies inside it (POSIX
    form); otherwise a normalised copy of ``path``."""
    p = path.strip()
    if not p:
        return p
    if os.path.isabs(p):
        p = posixpath.normpath(p)
        if base_dir is not None:
            for b in dict.fromkeys([os.path.abspath(base_dir), os.path.realpath(base_dir)]):
                b = posixpath.normpath(b)
                if p == b:
                    return "."
                if p.startswith(b.rstrip("/") + "/"):
                    return p[len(b.rstrip("/")) + 1 :]
        return p
    p = posixpath.normpath(p)
    return p[2:] if p.startswith("./") else p


def _split_option(msg: str) -> tuple[str, str | None, bool]:
    """Split a trailing ``[...]`` option group off ``msg``.

    Returns ``(message, rule, recognised)`` where ``rule`` is the -W / -R flag
    (``-Werror`` itself is skipped) or an analyzer checker name; ``recognised``
    says whether the bracket looked like an option group at all (only then is
    it removed from the message).
    """
    m = _OPT_RE.match(msg)
    if not m:
        return msg, None, False
    items = [i.strip() for i in m.group("opt").split(",") if i.strip()]
    rule: str | None = None
    recognised = False
    for item in items:
        if item.startswith(("-W", "-R", "-f")) or _CHECKER_RE.match(item):
            recognised = True
        if rule is not None:
            continue
        if item == "-Werror":
            continue
        if item.startswith("-Werror="):
            rule = "-W" + item[len("-Werror=") :]
        elif item.startswith(("-W", "-R")) or _CHECKER_RE.match(item):
            rule = item
    if not recognised:
        return msg, None, False
    return m.group("msg").rstrip(), rule, True


def _severity(sev: str) -> str:
    return {"fatal error": "error", "error": "error", "warning": "warning", "note": "note", "remark": "info"}[sev]


def _default_rule(sev: str) -> str | None:
    return {
        "fatal error": "clang-fatal",
        "error": "clang-error",
        "warning": "clang-warning",
        "remark": "clang-remark",
        "note": None,
    }[sev]


def parse_clang_diagnostics(text: str, tool: str = "clang", *, base_dir: Path | None = None) -> list[Diagnostic]:
    """Parse clang (driver, frontend, analyzer text) and linker output.

    Recognised shapes::

        src/a.c:12:5: warning: unused variable 'x' [-Wunused-variable]
        src/a.c:3:10: fatal error: 'nosuch.h' file not found
        src/a.c: error: ...                       (file, no line)
        clang: error: no such file or directory: 'x.c'   (driver, no location)
        error: no analyzer checkers ...           (bare)
        /usr/bin/ld: x.o: in function `main':     (context for the next line)
        main.c:(.text+0x10): undefined reference to `foo'
        /usr/bin/ld: cannot find -lfoo: No such file or directory
        ld.lld: error: undefined symbol: foo      (+ ">>> referenced by" lines)
        clang: error: linker command failed with exit code 1

    ``rule_id`` is the ``[-Wflag]`` (``-Werror,`` prefix dropped) or the
    analyzer checker in brackets when present, else ``clang-error`` /
    ``clang-fatal`` / ``clang-warning`` / ``clang-remark``; linker problems get
    ``linker-error`` (or ``linker-warning``).  ``note:`` lines become
    :class:`PathStep` entries of the preceding diagnostic (see module doc).
    ``base_dir`` (normally the snapshot directory) is used to turn absolute
    paths inside it into relative ones.
    """
    diags: list[Diagnostic] = []
    current: Diagnostic | None = None
    pending_raw: list[str] = []  # "In file included from" lines for the next diagnostic
    ld_function: str | None = None

    def emit(d: Diagnostic) -> None:
        nonlocal current
        if pending_raw:
            d.raw = "\n".join([*pending_raw, d.raw or ""])
            pending_raw.clear()
        diags.append(d)
        current = d

    def note(loc: Location | None, msg: str, line: str) -> None:
        # An include stack printed just before a note belongs to that note,
        # not to the next diagnostic.
        text = "\n".join([*pending_raw, line])
        pending_raw.clear()
        if current is not None:
            current.path.append(PathStep(location=loc, message=msg))
            current.raw = f"{current.raw}\n{text}" if current.raw else text
        else:
            d = Diagnostic(tool=tool, rule_id=None, severity="note", message=msg, location=loc, raw=text)
            diags.append(d)

    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip("\r\n")
        stripped = line.strip()
        if not stripped:
            continue
        if _SUMMARY_RE.match(stripped):
            current = None
            continue
        if _INCLUDED_RE.match(stripped) or (pending_raw and _INCLUDED_FROM_RE.match(line)):
            pending_raw.append(line)
            continue

        # ---- linker ------------------------------------------------------
        # A "file:line[:col]: severity:" line is always a compiler diagnostic;
        # never let its message text (which can quote source code) be
        # mistaken for linker output.
        if not _DIAG_RE.match(line):
            m = _LD_CONTEXT_RE.match(line)
            if m:
                ld_function = m.group("func")
                pending_raw.append(line)
                continue
            m = _LD_SYMREF_RE.match(line)
            if m:
                where = m.group("where")
                loc = Location(function=ld_function)
                wm = _LD_WHERE_RE.match(where)
                if wm and _SOURCE_EXT_RE.search(wm.group("file")):
                    loc.file = _rel_path(wm.group("file"), base_dir)
                    loc.line = int(wm.group("line")) if wm.group("line") else None
                emit(
                    Diagnostic(
                        tool=tool,
                        rule_id="linker-error",
                        severity="error",
                        message=m.group("msg"),
                        location=loc if (loc.file or loc.function) else None,
                        raw=line,
                    )
                )
                ld_function = None
                continue
            m = _LD_GENERIC_RE.match(line)
            if m:
                sev = m.group("sev") or "error"
                msg = m.group("msg")
                is_warning = sev == "warning"
                emit(
                    Diagnostic(
                        tool=tool,
                        rule_id="linker-warning" if is_warning else "linker-error",
                        severity="warning" if is_warning else "error",
                        message=msg,
                        raw=line,
                    )
                )
                continue
            m = _LLD_REF_RE.match(stripped)
            if m:
                note(None, m.group("msg"), line)
                continue

        # ---- compiler / analyzer -----------------------------------------
        loc: Location | None = None
        sev: str | None = None
        msg = ""
        driver_line = False
        m = _DIAG_RE.match(line)
        if m:
            sev, msg = m.group("sev"), m.group("msg")
            loc = Location(
                file=_rel_path(m.group("file"), base_dir),
                line=int(m.group("line")),
                column=int(m.group("col")) if m.group("col") else None,
            )
        else:
            m = _NOLINE_RE.match(line)
            if m:
                sev, msg = m.group("sev"), m.group("msg")
                prefix = m.group("file")
                if _DRIVER_RE.match(prefix):
                    driver_line = True
                else:
                    loc = Location(file=_rel_path(prefix, base_dir))
            else:
                m = _BARE_RE.match(line)
                if m:
                    sev, msg = m.group("sev"), m.group("msg")
        if sev is None:
            # Continuation (caret / source excerpt / analyzer extra lines): keep
            # it with the diagnostic it belongs to instead of dropping it.
            if current is not None and (line[:1].isspace() or stripped.startswith("|")):
                current.raw = f"{current.raw}\n{line}" if current.raw else line
            continue

        if sev == "note":
            message, _rule, _ = _split_option(msg)
            note(loc, message, line)
            continue

        message, rule, _ = _split_option(msg)
        if rule is None:
            rule = _default_rule(sev)
            if driver_line and "linker command failed" in message:
                rule = "linker-error"
        emit(Diagnostic(tool=tool, rule_id=rule, severity=_severity(sev), message=message, location=loc, raw=line))
    return diags


# --------------------------------------------------------------------------- command


def _define_args(defines: list[str]) -> list[str]:
    return [d if d.startswith("-D") else f"-D{d}" for d in defines]


def compile_command(ctx: BackendContext, source_rel: str, obj_path: Path) -> list[str]:
    """argv compiling ``source_rel`` (relative to the snapshot) to ``obj_path``.

    The spec's flags come first; Lean C's diagnostic-format flags come after
    them so a spec cannot (accidentally) re-enable colour or caret output that
    would break parsing.  The output path is canonicalised (``realpath``)
    because the sandbox binds the build directory at its *real* path: a
    symlinked run directory would otherwise be invisible to the compiler.
    """
    clang = ctx.tools.clang
    if not clang:
        raise ValueError("no C compiler available (ctx.tools.clang is None)")
    spec = ctx.spec
    return [
        clang,
        f"-std={spec.language.standard}",
        *spec.compile.flags,
        *_define_args(spec.compile.defines),
        *ctx.include_args(),
        *DIAGNOSTIC_FLAGS,
        "-c",
        source_rel,
        "-o",
        os.path.realpath(obj_path),
    ]


# --------------------------------------------------------------------------- helpers shared with analyzer


def safe_name(rel: str) -> str:
    """A flat, filesystem-safe name for a relative path (used for logs / raw files)."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in rel)[:80]


def crash_signal(signal: int | None, exit_code: int | None) -> int | None:
    """The signal that killed the tool, if any.  Inside bubblewrap a child
    killed by signal N is reported as exit status 128+N (clang itself never
    exits with a status above 128)."""
    if signal is not None:
        return signal
    if exit_code is not None and 128 < exit_code < 128 + 65:
        return exit_code - 128
    return None


def looks_like_crash(proc_stderr: str, signal: int | None, exit_code: int | None = None) -> bool:
    """Whether clang itself crashed (as opposed to rejecting the input).

    Crash markers are only looked for outside ``file:line: severity:``
    diagnostic lines: those can quote source text (``#warning PLEASE submit a
    bug report``), and code under test must not be able to turn its own
    compile error into a "compiler crash"."""
    if crash_signal(signal, exit_code) is not None:
        return True
    for line in (proc_stderr or "").splitlines():
        if _DIAG_RE.match(line):
            continue
        if any(marker in line for marker in _CRASH_MARKERS):
            return True
    return False


def crash_reason(proc_stderr: str, signal: int | None, exit_code: int | None = None) -> str:
    sig = crash_signal(signal, exit_code)
    return f"killed by signal {sig}" if sig is not None else "crash reported on stderr"


def unique_id(base: str, seen: set[str]) -> str:
    out, n = base, 2
    while out in seen:
        out = f"{base}#{n}"
        n += 1
    seen.add(out)
    return out


def failure_from_diagnostic(
    d: Diagnostic,
    *,
    kind: str,
    tool: str,
    id_prefix: str,
    seen: set[str],
    raw_ref: str | None,
) -> FailureReport:
    loc = d.location
    where = "?"
    if loc is not None:
        where = ":".join(str(x) for x in (loc.file or "?", loc.line, loc.column) if x is not None)
    fid = unique_id(f"{id_prefix}:{d.rule_id or 'unknown'}:{where}", seen)
    return FailureReport(
        id=fid,
        kind=kind,  # type: ignore[arg-type]
        tool=tool,
        property_id=d.rule_id,
        description=d.message,
        location=loc,
        trace=[TraceStep(kind="location", location=s.location, text=s.message) for s in d.path],
        raw_ref=raw_ref,
    )


def _first_error_text(diags: list[Diagnostic]) -> str:
    for d in diags:
        if d.severity == "error":
            where = d.location.short() if d.location else ""
            return f"{where}: {d.message}" if where else d.message
    return ""


# --------------------------------------------------------------------------- stage


def run_compile(ctx: BackendContext) -> StageResult:
    """Compile every ``spec.sources.editable`` file from the snapshot into
    ``build_dir/obj``.

    Outcome rules (definite evidence dominates): any compile error located in
    a source file, missing source or (with ``warnings_policy: block``) any
    warning -> ``FAIL`` (``COMPILE_ERROR``); otherwise a timeout ->
    ``TIMEOUT``; otherwise a compiler crash / launch failure / unexplained
    non-zero exit / errors with no source location (driver or infrastructure
    problems, e.g. an unknown flag in the spec) -> ``ERROR``; otherwise
    ``PASS`` (warnings, if any, are kept as diagnostics).
    """
    t0 = time.monotonic()
    spec = ctx.spec
    policy = spec.compile.warnings_policy
    details: dict = {
        "compiler": ctx.tools.clang,
        "compiler_version": ctx.tools.clang_version,
        "requested_compiler": spec.compile.compiler,
        "standard": spec.language.standard,
        "flags": list(spec.compile.flags),
        "defines": list(spec.compile.defines),
        "warnings_policy": policy,
        "sandbox": ctx.sandbox.kind,
        "files": [],
        "errors": 0,
        "warnings": 0,
    }

    def result(outcome: Outcome, summary: str, **kw) -> StageResult:
        return StageResult(
            stage=STAGE,
            outcome=outcome,
            status=outcome_to_status(STAGE, outcome),
            summary=summary,
            details=details,
            duration_s=round(time.monotonic() - t0, 3),
            **kw,
        )

    if not ctx.tools.clang:
        return result(
            Outcome.ERROR,
            f"C compiler not found (spec.compile.compiler={spec.compile.compiler!r}); "
            "install clang or point LEANC_CLANG at it",
        )
    version = ctx.tools.clang_version
    if version and "clang" not in version.lower():
        return result(
            Outcome.ERROR,
            f"compiler {ctx.tools.clang} does not identify as clang ({version!r}); "
            "Lean C's compile gate requires clang",
        )

    snap = Path(os.path.realpath(ctx.snapshot_dir))
    obj_root = ctx.build_dir / "obj"
    raw_root = ctx.raw_dir / "compile"
    obj_root.mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)
    limits = Limits(timeout_s=ctx.timeout(spec.compile.timeout_s))

    diagnostics: list[Diagnostic] = []
    failures: list[FailureReport] = []
    invocations = []
    seen_ids: set[str] = set()
    timeouts: list[str] = []
    tool_errors: list[str] = []
    missing: list[str] = []
    compiled = 0
    n_errors = n_warnings = 0
    error_diags: list[Diagnostic] = []

    for idx, rel in enumerate(spec.sources.editable):
        src = snap / rel
        entry: dict = {"source": rel}
        details["files"].append(entry)
        if not src.is_file():
            missing.append(rel)
            msg = (
                f"editable source {rel!r} is missing from the verification snapshot "
                "(it was never generated/written, or is not a regular file)"
            )
            entry["missing"] = True
            d = Diagnostic(tool="leanc", rule_id="missing-source", severity="error", message=msg, location=Location(file=rel))
            diagnostics.append(d)
            failures.append(
                FailureReport(
                    id=unique_id(f"compile:missing-source:{rel}", seen_ids),
                    kind="compile",
                    tool="leanc",
                    property_id="missing-source",
                    description=msg,
                    location=Location(file=rel),
                )
            )
            continue

        obj = obj_root / (rel[:-2] + ".o" if rel.endswith(".c") else rel + ".o")
        obj.parent.mkdir(parents=True, exist_ok=True)
        if obj.exists():
            obj.unlink()  # never let a stale object masquerade as this run's output
        argv = compile_command(ctx, rel, obj)
        proc = ctx.run(argv, limits=limits, cwd=snap)
        invocations.append(proc.to_invocation("clang", ctx.run_dir, ctx.log_name(STAGE, rel)))
        raw_path = raw_root / f"{idx:02d}-{safe_name(rel)}.stderr.txt"
        raw_path.write_text(redact(proc.stderr), encoding="utf-8")
        raw_ref = ctx.relrun(raw_path)
        entry.update(
            {
                "argv": proc.display_argv or argv,
                "exit_code": proc.exit_code,
                "signal": proc.signal,
                "timed_out": proc.timed_out,
                "output_truncated": proc.output_truncated,
                "raw": raw_ref,
            }
        )

        if proc.error:
            tool_errors.append(f"{rel}: {proc.error}")
            entry["error"] = proc.error
            continue
        if proc.timed_out:
            timeouts.append(rel)
            continue

        diags = parse_clang_diagnostics(proc.stderr, tool="clang", base_dir=snap)
        diagnostics.extend(diags)
        errs = [d for d in diags if d.severity == "error"]
        warns = [d for d in diags if d.severity == "warning"]
        entry["errors"], entry["warnings"] = len(errs), len(warns)

        if looks_like_crash(proc.stderr, proc.signal, proc.exit_code):
            why = crash_reason(proc.stderr, proc.signal, proc.exit_code)
            tool_errors.append(f"{rel}: clang crashed ({why})")
            entry["crashed"] = True
            continue
        if errs and not any(d.location is not None and d.location.file for d in errs):
            # Only driver / infrastructure errors (an unknown flag in the spec,
            # an unwritable output file, ...): nothing points at the code under
            # test, so this is a tool problem, not a COMPILE_ERROR to repair.
            tool_errors.append(f"{rel}: clang error not attributable to the source: {errs[0].message}")
            entry["driver_errors"] = [d.message for d in errs]
            continue

        n_errors += len(errs)
        n_warnings += len(warns)
        error_diags.extend(errs)
        for d in errs:
            failures.append(
                failure_from_diagnostic(d, kind="compile", tool="clang", id_prefix="clang", seen=seen_ids, raw_ref=raw_ref)
            )
        if policy == "block":
            for d in warns:
                failures.append(
                    failure_from_diagnostic(
                        d, kind="compile", tool="clang", id_prefix="clang", seen=seen_ids, raw_ref=raw_ref
                    )
                )

        if proc.exit_code != 0 and not errs:
            tool_errors.append(
                f"{rel}: clang exited with code {proc.exit_code} without reporting an error"
                + (" (output truncated)" if proc.output_truncated else "")
            )
            continue
        if proc.exit_code == 0 and not errs:
            if not obj.is_file():
                tool_errors.append(f"{rel}: clang reported success but produced no object file")
                continue
            compiled += 1
            entry["object"] = ctx.relrun(obj)

    details["errors"] = n_errors
    details["warnings"] = n_warnings
    details["compiled"] = compiled
    details["missing_sources"] = missing
    details["timeouts"] = timeouts
    details["tool_errors"] = tool_errors
    kw = dict(diagnostics=diagnostics, failures=failures, invocations=invocations)

    extra = []
    if timeouts:
        extra.append(f"timed out: {', '.join(timeouts)}")
    if tool_errors:
        extra.append(f"tool errors: {'; '.join(tool_errors)}")
    tail = f" [{'; '.join(extra)}]" if extra else ""

    if n_errors or missing:
        parts = []
        if n_errors:
            parts.append(f"{n_errors} error(s)")
        if missing:
            parts.append(f"missing source(s): {', '.join(missing)}")
        first = _first_error_text(error_diags)
        summary = f"compilation failed: {', '.join(parts)}, {n_warnings} warning(s)"
        if first:
            summary += f"; first: {first}"
        return result(Outcome.FAIL, summary + tail, **kw)
    if policy == "block" and n_warnings:
        return result(
            Outcome.FAIL,
            f"compilation produced {n_warnings} warning(s), blocked by policy (compile.warnings_policy=block)" + tail,
            **kw,
        )
    if timeouts:
        return result(
            Outcome.TIMEOUT,
            f"clang timed out after {limits.timeout_s:g}s compiling {', '.join(timeouts)}"
            + (f" [tool errors: {'; '.join(tool_errors)}]" if tool_errors else ""),
            **kw,
        )
    if tool_errors:
        return result(Outcome.ERROR, "compiler failure: " + "; ".join(tool_errors), **kw)
    return result(
        Outcome.PASS,
        f"compiled {compiled} file(s) with {version or ctx.tools.clang}: 0 errors, {n_warnings} warning(s)"
        f" (warnings_policy={policy})",
        **kw,
    )
