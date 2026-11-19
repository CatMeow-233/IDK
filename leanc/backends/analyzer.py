"""Clang Static Analyzer gate.

The analyzer is run once per editable translation unit with SARIF output
(machine-readable, with the full bug path in ``codeFlows``).  SARIF is the
primary source of findings; the analyzer's textual stderr is used

* as a consistency check (a finding printed on stderr but missing from the
  SARIF file is added, and the discrepancy noted), and
* as a fallback when SARIF output is unsupported, missing or unparsable: the
  file is re-analysed with ``-Xclang -analyzer-output=text`` (which prints the
  bug path as ``note:`` lines) and ``details["format"]`` records ``"text"``.

Nothing is ever silently dropped: a tool problem is ``Outcome.ERROR`` and every
fallback is recorded in ``details["notes"]``.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from leanc.backends.clang import (
    DIAGNOSTIC_FLAGS,
    _define_args,
    _rel_path,
    crash_reason,
    failure_from_diagnostic,
    looks_like_crash,
    parse_clang_diagnostics,
    safe_name,
    unique_id,
)
from leanc.core.context import BackendContext
from leanc.core.models import Diagnostic, FailureReport, Location, PathStep, StageResult
from leanc.core.redact import redact
from leanc.core.runner import Limits
from leanc.core.status import Outcome, outcome_to_status

STAGE = "static_analysis"
TOOL = "clang-static-analyzer"

#: Driver arguments selecting SARIF output (clang >= 15).  Module-level so the
#: fallback path can be exercised in tests.
SARIF_ARGS: tuple[str, ...] = ("--analyzer-output", "sarif")
#: Arguments selecting textual output (bug path printed as notes on stderr).
TEXT_ARGS: tuple[str, ...] = ("-Xclang", "-analyzer-output=text")

_CHECKER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*$")
_SARIF_UNSUPPORTED_RE = re.compile(
    r"(unknown argument|unsupported option|invalid value|unsupported value)[^\n]*(analyzer-output|sarif)",
    re.I,
)
_MAX_RAW = 4000

_LEVELS = {"error": "error", "warning": "warning", "note": "note", "none": "info"}

# --------------------------------------------------------------------------- SARIF


def _d(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _l(x: Any) -> list:
    return x if isinstance(x, list) else []


def _int(x: Any) -> int | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, str) and x.isdigit():
        return int(x)
    return None


def _uri_to_path(uri: str, uri_base_id: str | None, base_ids: dict) -> str:
    """Turn a SARIF artifact URI into a filesystem path (absolute for
    ``file:`` URIs, otherwise relative, resolved against ``uriBaseId`` when
    the run defines it)."""
    if uri.startswith("file:"):
        # file:///abs/path, file:/abs/path, file://localhost/abs/path.  A
        # non-local host is not meaningful here; only the path is kept.
        return unquote(urlparse(uri).path)
    path = unquote(uri)
    if uri_base_id and not os.path.isabs(path):
        base = _d(base_ids.get(uri_base_id))
        base_uri = base.get("uri")
        if isinstance(base_uri, str) and base_uri:
            base_path = _uri_to_path(base_uri, base.get("uriBaseId"), {}) if base_uri.startswith("file:") else unquote(base_uri)
            path = posixpath.join(base_path, path)
    return path


def _message_text(msg: Any, rule: dict | None = None) -> str:
    """SARIF message: ``text``, else ``markdown``, else ``id`` looked up in the
    rule's ``messageStrings`` with ``{n}`` placeholders filled from
    ``arguments``."""
    m = _d(msg)
    for key in ("text", "markdown"):
        v = m.get(key)
        if isinstance(v, str) and v:
            return v
    mid = m.get("id")
    if isinstance(mid, str) and rule:
        tmpl = _d(_d(rule.get("messageStrings")).get(mid)).get("text")
        if isinstance(tmpl, str):
            args = [str(a) for a in _l(m.get("arguments"))]

            def sub(mm: re.Match) -> str:
                i = int(mm.group(1))
                return args[i] if i < len(args) else mm.group(0)

            return re.sub(r"\{(\d+)\}", sub, tmpl)
    return ""


class _RunCtx:
    def __init__(self, run: dict, base_dir: Path | None):
        self.base_dir = base_dir
        self.artifacts = _l(run.get("artifacts"))
        self.rules = _l(_d(_d(run.get("tool")).get("driver")).get("rules"))
        self.base_ids = _d(run.get("originalUriBaseIds"))

    def rule(self, result: dict) -> dict:
        idx = _int(result.get("ruleIndex"))
        if idx is None:
            idx = _int(_d(result.get("rule")).get("index"))
        if idx is not None and 0 <= idx < len(self.rules):
            return _d(self.rules[idx])
        rid = result.get("ruleId") or _d(result.get("rule")).get("id")
        if isinstance(rid, str):
            for r in self.rules:
                if _d(r).get("id") == rid:
                    return _d(r)
        return {}

    def location(self, loc: Any) -> Location | None:
        loc = _d(loc)
        phys = _d(loc.get("physicalLocation"))
        art = _d(phys.get("artifactLocation"))
        uri = art.get("uri")
        base_id = art.get("uriBaseId")
        if not isinstance(uri, str) or not uri:
            idx = _int(art.get("index"))
            if idx is not None and 0 <= idx < len(self.artifacts):
                aloc = _d(_d(self.artifacts[idx]).get("location"))
                uri = aloc.get("uri")
                base_id = base_id or aloc.get("uriBaseId")
        file = None
        if isinstance(uri, str) and uri:
            file = _rel_path(_uri_to_path(uri, base_id if isinstance(base_id, str) else None, self.base_ids), self.base_dir)
        region = _d(phys.get("region"))
        line = _int(region.get("startLine"))
        col = _int(region.get("startColumn"))
        func = None
        for ll in _l(loc.get("logicalLocations")):
            ll = _d(ll)
            name = ll.get("fullyQualifiedName") or ll.get("name")
            if isinstance(name, str) and name and (ll.get("kind") in (None, "function", "member")):
                func = name
                break
        if file is None and line is None and func is None:
            return None
        return Location(file=file, line=line, column=col, function=func)


def parse_sarif(
    data: dict, *, base_dir: Path | None = None, tool: str = TOOL
) -> list[Diagnostic]:
    """Convert a SARIF 2.1.0 log into diagnostics.

    * ``ruleId`` (or ``ruleIndex`` into ``tool.driver.rules``) -> ``rule_id``;
    * ``level`` (or the rule's ``defaultConfiguration.level``, default
      ``warning``) -> severity (``none`` -> ``info``);
    * first ``locations[].physicalLocation`` -> location; ``file://`` URIs and
      artifact-index references are resolved, and paths inside ``base_dir``
      are made relative to it;
    * ``codeFlows[].threadFlows[].locations[]`` -> :class:`PathStep` list.

    Missing or malformed fields are tolerated (they just yield less
    information); a non-dict input yields no diagnostics.
    """
    out: list[Diagnostic] = []
    for run in _l(_d(data).get("runs")):
        run = _d(run)
        rc = _RunCtx(run, base_dir)
        for res in _l(run.get("results")):
            if not isinstance(res, dict):
                continue
            rule = rc.rule(res)
            rule_id = res.get("ruleId")
            if not isinstance(rule_id, str) or not rule_id:
                rule_id = _d(res.get("rule")).get("id") or rule.get("id")
            if not isinstance(rule_id, str) or not rule_id:
                rule_id = None
            level = res.get("level")
            if not isinstance(level, str):
                level = _d(rule.get("defaultConfiguration")).get("level")
            severity = _LEVELS.get(level if isinstance(level, str) else "warning", "warning")
            message = _message_text(res.get("message"), rule) or (
                _message_text(rule.get("shortDescription")) or (rule_id or "static analysis finding")
            )
            location = None
            for loc in _l(res.get("locations")):
                location = rc.location(loc)
                if location is not None:
                    break
            path: list[PathStep] = []
            for cf in _l(res.get("codeFlows")):
                for tf in _l(_d(cf).get("threadFlows")):
                    for tfl in _l(_d(tf).get("locations")):
                        tfl = _d(tfl)
                        sloc = _d(tfl.get("location"))
                        step_msg = _message_text(sloc.get("message")) or _message_text(tfl.get("message"))
                        path.append(PathStep(location=rc.location(sloc), message=step_msg))
            if location is None and path:
                location = path[-1].location
            try:
                raw = json.dumps(res, sort_keys=True)
            except (TypeError, ValueError):
                raw = None
            if raw and len(raw) > _MAX_RAW:
                raw = raw[:_MAX_RAW] + "...[truncated]"
            out.append(
                Diagnostic(
                    tool=tool,
                    rule_id=rule_id,
                    severity=severity,  # type: ignore[arg-type]
                    message=message,
                    location=location,
                    path=path,
                    raw=raw,
                )
            )
    return out


# --------------------------------------------------------------------------- command


def analyzer_command(
    ctx: BackendContext, source_rel: str, output_path: Path | None, *, output_format: str = "sarif"
) -> list[str]:
    """argv analysing ``source_rel`` (relative to the snapshot).

    ``output_format="sarif"`` writes SARIF to ``output_path``;
    ``"text"`` prints the findings and their bug paths on stderr.
    """
    clang = ctx.tools.clang
    if not clang:
        raise ValueError("no clang available (ctx.tools.clang is None)")
    spec = ctx.spec
    argv = [clang, "--analyze"]
    if output_format == "sarif":
        if output_path is None:
            raise ValueError("SARIF output needs an output path")
        # realpath: the sandbox binds the build directory at its real path.
        argv += [*SARIF_ARGS, "-o", os.path.realpath(output_path)]
    elif output_format == "text":
        argv += [*TEXT_ARGS, "-o", os.devnull]
    else:
        raise ValueError(f"unknown analyzer output format {output_format!r}")
    argv += [
        f"-std={spec.language.standard}",
        *_define_args(spec.compile.defines),
        *ctx.include_args(),
        *DIAGNOSTIC_FLAGS,
    ]
    for checker in spec.static_analysis.extra_checkers:
        argv += ["-Xclang", f"-analyzer-checker={checker}"]
    argv.append(source_rel)
    return argv


def _is_finding(d: Diagnostic) -> bool:
    """A textual analyzer diagnostic that reports a bug (rule = checker name),
    as opposed to a driver / frontend message."""
    rid = d.rule_id or ""
    return d.severity in ("warning", "error") and bool(rid) and not rid.startswith(("-W", "-R", "clang-", "linker-"))


def _load_sarif(path: Path) -> tuple[dict | None, str]:
    if not path.exists():
        return None, "SARIF file was not produced"
    if not path.is_file():
        return None, "SARIF output path is not a regular file"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"cannot read SARIF file: {exc}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"SARIF file is not valid JSON: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        return None, "SARIF file has no 'runs' array"
    return data, ""


def _key(d: Diagnostic) -> tuple:
    loc = d.location or Location()
    return (d.rule_id, loc.file, loc.line, loc.column, d.message)


# --------------------------------------------------------------------------- stage


def run_static_analysis(ctx: BackendContext) -> StageResult:
    """Run the Clang Static Analyzer on every editable source.

    Findings are always returned as diagnostics.  With
    ``static_analysis.policy: block`` any finding makes the stage ``FAIL``
    (``STATIC_ANALYSIS_WARNING``) with one ``FailureReport(kind="static")``
    each; with ``report`` the stage passes and findings are informational.
    Otherwise: timeout -> ``TIMEOUT``; analyzer crash, launch failure,
    non-zero exit with errors (e.g. code that does not compile, unknown
    checker) -> ``ERROR``.
    """
    t0 = time.monotonic()
    spec = ctx.spec
    sa = spec.static_analysis
    policy = sa.policy
    details: dict = {
        "clang": ctx.tools.clang,
        "clang_version": ctx.tools.clang_version,
        "policy": policy,
        "checkers": {"default": "clang default checker set", "extra": list(sa.extra_checkers)},
        "format": None,
        "files": [],
        "findings": 0,
        "notes": [],
        "sandbox": ctx.sandbox.kind,
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
            f"clang not found (spec.compile.compiler={spec.compile.compiler!r}); the static analyzer "
            "is part of clang -- install clang or point LEANC_CLANG at it",
        )
    version = ctx.tools.clang_version
    if version and "clang" not in version.lower():
        return result(Outcome.ERROR, f"{ctx.tools.clang} does not identify as clang ({version!r}); cannot run the Clang Static Analyzer")
    bad = [c for c in sa.extra_checkers if not _CHECKER_NAME_RE.match(c)]
    if bad:
        return result(Outcome.ERROR, f"invalid static_analysis.extra_checkers name(s): {bad}")

    snap = Path(os.path.realpath(ctx.snapshot_dir))
    out_root = ctx.build_dir / "analyzer"
    raw_root = ctx.raw_dir / "static_analysis"
    out_root.mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)
    limits = Limits(timeout_s=ctx.timeout(sa.timeout_s))

    diagnostics: list[Diagnostic] = []
    findings: list[tuple[Diagnostic, str | None]] = []  # (finding, raw_ref)
    seen_keys: set[tuple] = set()
    invocations = []
    timeouts: list[str] = []
    tool_errors: list[str] = []
    formats: set[str] = set()
    notes: list[str] = details["notes"]

    def add_findings(items: list[Diagnostic], raw_ref: str | None) -> int:
        n = 0
        for d in items:
            k = _key(d)
            if k in seen_keys:  # same finding reached via two TUs (e.g. in a header)
                continue
            seen_keys.add(k)
            diagnostics.append(d)
            findings.append((d, raw_ref))
            n += 1
        return n

    def run_one(argv: list[str], what: str):
        proc = ctx.run(argv, limits=limits, cwd=snap)
        invocations.append(proc.to_invocation("clang-static-analyzer", ctx.run_dir, ctx.log_name(STAGE, what)))
        return proc

    def proc_problem(proc, rel: str, diags: list[Diagnostic]) -> str | None:
        """Describe why this analyzer run cannot be trusted (None if fine)."""
        if proc.error:
            return f"{rel}: {proc.error}"
        if looks_like_crash(proc.stderr, proc.signal, proc.exit_code):
            return f"{rel}: analyzer crashed ({crash_reason(proc.stderr, proc.signal, proc.exit_code)})"
        errs = [d for d in diags if d.severity == "error" and not _is_finding(d)]
        if proc.exit_code != 0 or errs:
            first = ""
            if errs:
                e = errs[0]
                first = f": {(e.location.short() + ': ') if e.location else ''}{e.message}"
            return f"{rel}: analyzer failed (exit {proc.exit_code}){first}"
        return None

    for idx, rel in enumerate(spec.sources.editable):
        entry: dict = {"source": rel}
        details["files"].append(entry)
        if not (snap / rel).is_file():
            tool_errors.append(f"{rel}: source missing from the verification snapshot; not analysed")
            entry["missing"] = True
            continue
        stem = f"{idx:02d}-{safe_name(rel)}"
        sarif_path = out_root / f"{stem}.sarif"
        if sarif_path.is_dir():
            shutil.rmtree(sarif_path)
        elif sarif_path.exists():
            sarif_path.unlink()

        argv = analyzer_command(ctx, rel, sarif_path, output_format="sarif")
        proc = run_one(argv, f"{rel}.sarif")
        entry.update({"argv": proc.display_argv or argv, "exit_code": proc.exit_code, "timed_out": proc.timed_out})
        if proc.timed_out:
            timeouts.append(rel)
            continue
        stderr_diags = parse_clang_diagnostics(proc.stderr, tool=TOOL, base_dir=snap)

        use_text = False
        why = ""
        problem = proc_problem(proc, rel, stderr_diags)
        if problem and not proc.error and _SARIF_UNSUPPORTED_RE.search(proc.stderr or ""):
            use_text, why = True, "this clang does not support SARIF analyzer output"
        elif problem:
            tool_errors.append(problem)
            diagnostics.extend(d for d in stderr_diags if d.severity == "error")
            continue
        else:
            data, why = _load_sarif(sarif_path)
            if data is None:
                use_text = True
                # e.g. "warning: could not create file: ..." (clang still exits 0)
                said = [d.message for d in stderr_diags if not _is_finding(d) and d.severity in ("error", "warning")]
                if said:
                    why += f" (clang said: {said[0]})"
            else:
                raw_copy = raw_root / f"{stem}.sarif"
                shutil.copyfile(sarif_path, raw_copy)
                raw_ref = ctx.relrun(raw_copy)
                entry["sarif"] = raw_ref
                sarif_findings = parse_sarif(data, base_dir=snap, tool=TOOL)
                n = add_findings(sarif_findings, raw_ref)
                # Consistency check against what the analyzer printed.
                sarif_keys = {(d.rule_id, (d.location or Location()).file, (d.location or Location()).line) for d in sarif_findings}
                missing_in_sarif = [
                    d
                    for d in stderr_diags
                    if _is_finding(d) and (d.rule_id, (d.location or Location()).file, (d.location or Location()).line) not in sarif_keys
                ]
                if missing_in_sarif:
                    stderr_raw = raw_root / f"{stem}.stderr.txt"
                    stderr_raw.write_text(redact(proc.stderr), encoding="utf-8")
                    n += add_findings(missing_in_sarif, ctx.relrun(stderr_raw))
                    notes.append(
                        f"{rel}: {len(missing_in_sarif)} finding(s) printed by the analyzer were missing from "
                        "the SARIF file and were taken from its textual output"
                    )
                entry["format"] = "sarif"
                entry["findings"] = n
                formats.add("sarif")
                continue

        # ---- textual fallback -------------------------------------------
        notes.append(f"{rel}: SARIF output unusable ({why}); fell back to textual analyzer output")
        targv = analyzer_command(ctx, rel, None, output_format="text")
        tproc = run_one(targv, f"{rel}.text")
        entry.update(
            {"fallback_reason": why, "text_argv": tproc.display_argv or targv, "text_exit_code": tproc.exit_code}
        )
        if tproc.timed_out:
            timeouts.append(rel)
            continue
        tdiags = parse_clang_diagnostics(tproc.stderr, tool=TOOL, base_dir=snap)
        problem = proc_problem(tproc, rel, tdiags)
        if problem:
            tool_errors.append(problem + " (textual fallback)")
            diagnostics.extend(d for d in tdiags if d.severity == "error")
            continue
        raw_txt = raw_root / f"{stem}.analyzer.txt"
        raw_txt.write_text(redact(tproc.stderr), encoding="utf-8")
        raw_ref = ctx.relrun(raw_txt)
        entry["text"] = raw_ref
        entry["format"] = "text"
        entry["findings"] = add_findings([d for d in tdiags if _is_finding(d)], raw_ref)
        formats.add("text")

    details["format"] = (formats.pop() if len(formats) == 1 else "mixed") if formats else None
    details["findings"] = len(findings)
    details["by_rule"] = _count_by_rule(f for f, _ in findings)
    details["timeouts"] = timeouts
    details["tool_errors"] = tool_errors

    failures: list[FailureReport] = []
    if policy == "block":
        seen_ids: set[str] = set()
        for d, raw_ref in findings:
            failures.append(
                failure_from_diagnostic(d, kind="static", tool=TOOL, id_prefix="static", seen=seen_ids, raw_ref=raw_ref)
            )
    kw = dict(diagnostics=diagnostics, failures=failures, invocations=invocations)

    extra = []
    if timeouts:
        extra.append(f"timed out: {', '.join(timeouts)}")
    if tool_errors:
        extra.append(f"tool errors: {'; '.join(tool_errors)}")
    if details["format"] and details["format"] != "sarif":
        extra.append(f"format: {details['format']}")
    tail = f" [{'; '.join(extra)}]" if extra else ""
    n = len(findings)
    first = ""
    if findings:
        d0 = findings[0][0]
        first = f"; first: {d0.location.short() + ': ' if d0.location else ''}{d0.message} [{d0.rule_id}]"

    if policy == "block" and n:
        return result(Outcome.FAIL, f"static analyzer reported {n} finding(s) (policy=block){first}{tail}", **kw)
    if timeouts:
        return result(
            Outcome.TIMEOUT,
            f"static analyzer timed out after {limits.timeout_s:g}s on {', '.join(timeouts)}{tail if tool_errors else ''}",
            **kw,
        )
    if tool_errors:
        return result(Outcome.ERROR, "static analyzer failure: " + "; ".join(tool_errors), **kw)
    if n:
        return result(
            Outcome.PASS,
            f"static analyzer reported {n} finding(s); not blocking (policy=report){first}{tail}",
            **kw,
        )
    return result(
        Outcome.PASS,
        f"static analyzer: no findings in {len(spec.sources.editable)} file(s){tail}",
        **kw,
    )


def _count_by_rule(items) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in items:
        k = d.rule_id or "unknown"
        out[k] = out.get(k, 0) + 1
    return out
