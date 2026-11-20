"""Sanitizer test gate: build the editable sources together with the trusted
test driver under AddressSanitizer / UndefinedBehaviorSanitizer /
LeakSanitizer, run every trusted test case (sandboxed, bounded) and turn
sanitizer reports and test-oracle mismatches into structured results.

Why this gate exists
--------------------
Static analysis over-approximates and CBMC is bounded by its harness; running
the real binary on concrete, human-approved inputs under sanitizers gives
*definite* evidence (a sanitizer report is never a false alarm about the
executed path).  The converse does not hold: sanitizers only observe the paths
the test cases execute, so a clean run is **not** a proof (recorded in
``details["limitation"]``).

Trust and isolation
-------------------
* The binary contains AI-generated code, so it is untrusted.  It is executed
  only if :meth:`BackendContext.may_execute_untrusted` allows it (functional
  sandbox, or explicit ``--allow-unsandboxed`` which is recorded as a warning).
* Inside the sandbox the program sees the binary's directory read-only and a
  fresh per-case working directory read-write -- nothing else of the run
  (not the snapshot, not ``raw/`` or other stages' build outputs).
* ASan reserves terabytes of virtual address space for its shadow memory, so
  ``RLIMIT_AS`` cannot be used with ASan/LSan; memory is bounded with
  ``hard_rss_limit_mb`` instead (``RLIMIT_AS`` is still applied to builds that
  do not use those runtimes).
* Editable sources that try to steer the sanitizers themselves
  (``__asan_default_options``, ``__lsan_is_turned_off``, ``no_sanitize``
  attributes, ``<sanitizer/...>`` interface headers, ``__has_feature(..._sanitizer)``)
  are reported as a ``leanc-policy`` failure: the measurement could be
  manipulated by the code being measured.

Exit codes
----------
ASan / UBSan / LSan are configured with exit codes 86 / 87 / 88.  In the
*combined* ASan+UBSan runtime all three share one ``exitcode`` common flag and
``UBSAN_OPTIONS`` is parsed last, so every sanitizer abort exits with 87.
Tool attribution therefore always comes from the parsed report, never from the
exit code.
"""

from __future__ import annotations

import glob
import math
import os
import posixpath
import re
import shlex
import shutil
import signal as _signal
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from leanc.backends.cxx import compiler, compiler_missing, compiler_version
from leanc.core.context import BackendContext
from leanc.core.models import (
    CounterexampleInput,
    Diagnostic,
    FailureReport,
    Location,
    PathStep,
    StageResult,
    ToolInvocation,
)
from leanc.core.redact import redact
from leanc.core.runner import Limits, ProcResult
from leanc.core.sandbox import SandboxMounts
from leanc.core.status import Outcome, Status, outcome_to_status
from leanc.specs.model import TestCase

STAGE = "sanitizer"
LIMITATION = "sanitizers only observe the executed test paths; absence of findings is not a proof"

EXIT_ASAN = 86
EXIT_UBSAN = 87
EXIT_LSAN = 88
SANITIZER_EXIT_CODES = {EXIT_ASAN: "asan", EXIT_UBSAN: "ubsan", EXIT_LSAN: "lsan"}

#: Base flags of the sanitizer build (before the spec's own flags).
BASE_FLAGS: tuple[str, ...] = ("-g", "-O1", "-fno-omit-frame-pointer")
#: Appended after the spec's flags so clang's output stays parseable.
DIAGNOSTIC_FLAGS: tuple[str, ...] = (
    "-fno-color-diagnostics",
    "-fno-caret-diagnostics",
    "-fdiagnostics-show-option",
)

_RAW_EXCERPT_LINES = 60
_RAW_EXCERPT_CHARS = 6000
_CALL_STACK_MAX = 20

# =========================================================================== parsing

_TOOL_NAMES = {
    "AddressSanitizer": "asan",
    "LeakSanitizer": "lsan",
    "UndefinedBehaviorSanitizer": "ubsan",
}
_TOOL_TITLES = {v: k for k, v in _TOOL_NAMES.items()}

_ASAN_HDR_RE = re.compile(r"^==\d+==\s*ERROR: AddressSanitizer: (?P<rest>.+?)\s*$")
_LSAN_HDR_RE = re.compile(r"^==\d+==\s*ERROR: LeakSanitizer: (?P<rest>.+?)\s*$")
_RSS_HDR_RE = re.compile(
    r"^==\d+==\s*(?P<tool>AddressSanitizer|LeakSanitizer|UndefinedBehaviorSanitizer): "
    r"hard rss limit exhausted\s*(?P<rest>.*?)\s*$"
)
_UBSAN_RE = re.compile(r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+): runtime error: (?P<msg>.*?)\s*$")
# Location-less forms UBSan prints: "<unknown>", a bare file name, or "(module+0xoff)".
_UBSAN_NOLOC_RE = re.compile(
    r"^(?P<file><unknown>|\S+\.(?:c|h|cc|cpp|cxx|hpp|inc)|\([^()]*\+0x[0-9a-fA-F]+\)): runtime error: (?P<msg>.*?)\s*$"
)
_SUMMARY_RE = re.compile(
    r"^SUMMARY: (?P<tool>AddressSanitizer|LeakSanitizer|UndefinedBehaviorSanitizer): "
    r"(?P<kind>\S+)(?:\s+(?P<rest>.*?))?\s*$"
)
_LEAK_RE = re.compile(
    r"^(?P<kind>Direct|Indirect) leak of (?P<bytes>\d+) byte\(s\) in (?P<objs>\d+) object\(s\) allocated from:"
)
_FRAME_RE = re.compile(r"^\s*#(?P<n>\d+)\s+0x[0-9a-fA-F]+\s*(?P<rest>.*?)\s*$")
_BUILDID_RE = re.compile(r"\s*\(BuildId: [0-9a-fA-F]+\)\s*$")
_FILELOC_RE = re.compile(r"^(?P<file>[^\s]+?):(?P<line>\d+)(?::(?P<col>\d+))?$")
_MODULE_RE = re.compile(r"^(?P<func>.+?)\s+\((?P<module>[^()]*)\)$")
_SRCFILE_RE = re.compile(r"^(?:/\S+|\S+\.(?:c|h|cc|cpp|cxx|hpp|inc|S|s))$")
_ACCESS_RE = re.compile(r"^(?P<acc>READ|WRITE) of size (?P<size>\d+) at ")
_LOCATED_RE = re.compile(r"\bis located (?P<what>.+?)\s*$")
_SIGCAUSE_RE = re.compile(r"The signal is caused by a (?P<acc>\w+) memory access")
_HINT_RE = re.compile(r"^==\d+==\s*Hint: (?P<hint>.+?)\s*$")
_VAR_RE = re.compile(r"\[\d+, \d+\) '(?P<var>[^']+)'.*<== (?P<what>Memory access .+?)\s*$")
_CUT_RE = re.compile(r"^(?:Shadow bytes around the buggy address|==\d+==\s*Process memory map follows)")
_ADDR_PAREN_RE = re.compile(r"\s*\(0x[0-9a-fA-F]+\)|\s*\[0x[0-9a-fA-F]+,\s*0x[0-9a-fA-F]+\)")
_ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]{5,}\b")

#: Signs that the sanitizer *runtime* failed (a tool problem, not a finding).
_INTERNAL_ERROR_RE = re.compile(
    r"LeakSanitizer has encountered a fatal error"
    r"|Sanitizer CHECK failed"
    r"|AddressSanitizer: CHECK failed"
    r"|ReserveShadowMemoryRange failed"
    r"|Shadow memory range interleaves"
    r"|ERROR: AddressSanitizer failed to allocate"
    r"|ASan runtime does not come first"
    r"|ERROR: Failed to mmap"
)

#: UBSan "runtime error:" message -> rule id (first match wins; order matters).
_UBSAN_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), r)
    for p, r in (
        (r"signed integer overflow|negation of .* cannot be represented|division of .* by -1 cannot be represented",
         "signed-integer-overflow"),
        (r"unsigned integer overflow", "unsigned-integer-overflow"),
        (r"division by zero|remainder by zero", "division-by-zero"),
        (r"index .* out of bounds", "out-of-bounds-index"),
        (r"misaligned address|alignment assumption", "misaligned"),
        (r"shift exponent|left shift of|right shift of|shift of negative", "shift"),
        (r"applying (?:non-)?zero offset|pointer index expression|(?:addition|subtraction) of unsigned offset",
         "pointer-overflow"),
        (r"null pointer", "null-pointer"),
        (r"insufficient space for an object of type", "object-size"),
        (r"is not a valid value for type", "invalid-value"),
        (r"outside the range of representable values", "float-cast-overflow"),
        (r"variable length array bound", "vla-bound"),
        (r"unreachable program point", "unreachable"),
        (r"implicit conversion", "implicit-conversion"),
        (r"not a valid argument", "invalid-builtin-use"),
        (r"incorrect function type", "function-type-mismatch"),
        (r"end of a value-returning function", "missing-return"),
    )
)
UBSAN_FALLBACK_RULE = "ubsan-runtime-error"

_SYSTEM_PREFIXES = ("/usr/", "/lib/", "/lib32/", "/lib64/", "/libx32/", "/opt/", "/build/", "/proc/", "/nix/")
_GLIBC_DIRS = ("csu/", "sysdeps/", "nptl/", "stdlib/", "string/", "libio/", "malloc/", "elf/", "posix/", "misc/")


@dataclass
class StackFrame:
    """One ``#N 0x... in func file:line:col`` line of a sanitizer stack trace."""

    index: int
    function: str | None = None
    file: str | None = None
    line: int | None = None
    column: int | None = None
    module: str | None = None
    text: str = ""

    def short(self, display_file: str | None = None) -> str:
        where = display_file or self.file
        if where:
            loc = where + (f":{self.line}" if self.line is not None else "")
            if self.column is not None and self.line is not None:
                loc += f":{self.column}"
        else:
            loc = f"({self.module})" if self.module else "?"
        return f"#{self.index} {self.function or '??'} {loc}"


@dataclass
class SanitizerFinding:
    """A parsed sanitizer report: the :class:`Diagnostic` plus the stack
    (kept separately because ``Diagnostic`` has no call-stack field)."""

    diagnostic: Diagnostic
    frames: list[StackFrame] = field(default_factory=list)
    base_dir: str | None = None

    @property
    def tool(self) -> str:
        return self.diagnostic.tool

    @property
    def rule_id(self) -> str:
        return self.diagnostic.rule_id or "unknown"

    def call_stack(self) -> list[str]:
        out = []
        for fr in self.frames[:_CALL_STACK_MAX]:
            out.append(fr.short(_display_path(fr.file, self.base_dir) if fr.file else None))
        return out


@dataclass
class _Block:
    tool: str  # asan | lsan | ubsan
    start: int
    header: str = ""
    kind: str | None = None  # ASan bug type / rss
    ub_file: str | None = None
    ub_line: int | None = None
    ub_col: int | None = None
    ub_msg: str = ""
    summary_kind: str | None = None
    summary_rest: str | None = None
    end: int | None = None
    cut: int | None = None  # first line excluded from the raw excerpt
    stacks: list[list[StackFrame]] = field(default_factory=list)
    leaks: list[dict] = field(default_factory=list)
    access: str | None = None
    located: str | None = None
    sigcause: str | None = None
    hint: str | None = None
    variable: str | None = None
    closed: bool = False


def _parse_frame(line: str) -> StackFrame | None:
    m = _FRAME_RE.match(line)
    if not m:
        return None
    fr = StackFrame(index=int(m["n"]), text=line.strip())
    rest = _BUILDID_RE.sub("", m["rest"]).strip()
    if rest.startswith("in "):
        rest = rest[3:].strip()
        mm = _MODULE_RE.match(rest)
        if mm and ("+0x" in mm["module"] or mm["module"].startswith("<")):
            fr.function, fr.module = mm["func"], mm["module"]
            return fr
        parts = rest.rsplit(None, 1)
        if len(parts) == 2:
            fm = _FILELOC_RE.match(parts[1])
            if fm:
                fr.function = parts[0]
                fr.file = fm["file"]
                fr.line = int(fm["line"])
                fr.column = int(fm["col"]) if fm["col"] else None
                return fr
            if _SRCFILE_RE.match(parts[1]):
                # symbolised without line info (e.g. an implicit null check)
                fr.function, fr.file = parts[0], parts[1]
                return fr
        fr.function = rest or None
        return fr
    if rest.startswith("(") and rest.endswith(")"):
        fr.module = rest[1:-1]
    return fr


def _is_system_path(path: str) -> bool:
    p = path.replace("\\", "/")
    if not p or p.startswith("<") or p in ("??", "?"):
        return True
    if p.startswith("/"):
        return p.startswith(_SYSTEM_PREFIXES) or "/compiler-rt/" in p
    return p.startswith("../") or "/../" in p or p.startswith(_GLIBC_DIRS) or "/sysdeps/" in p


def _project_rel(path: str, project_files: set[str] | None, base_dir: str | None) -> str | None:
    """Workspace-relative name of ``path`` if it is a project file.

    Accepts the relative name itself, an absolute path under ``base_dir``
    (the snapshot the binary was built from), or -- tolerating symlinked or
    copied run directories -- any absolute path *ending* in ``/<project file>``.
    """
    p = path.replace("\\", "/")
    if base_dir:
        b = base_dir.rstrip("/")
        if p.startswith(b + "/"):
            rel = posixpath.normpath(p[len(b) + 1 :])
            if project_files is None or rel in project_files:
                return rel
    if not p.startswith("/"):
        rel = posixpath.normpath(p)
        if project_files is not None and rel in project_files:
            return rel
    if project_files:
        best = None
        for cand in project_files:
            if (p == cand or p.endswith("/" + cand)) and (best is None or len(cand) > len(best)):
                best = cand
        return best
    return None


def _display_path(path: str | None, base_dir: str | None) -> str | None:
    if not path:
        return None
    if base_dir:
        b = base_dir.rstrip("/")
        if path.startswith(b + "/"):
            return posixpath.normpath(path[len(b) + 1 :])
    return path


def _pick_frame(
    frames: list[StackFrame], project_files: set[str] | None, base_dir: str | None
) -> tuple[StackFrame | None, str | None]:
    """First frame in a project file; else the first frame with a non-system
    source path; else nothing."""
    for fr in frames:
        if fr.file:
            rel = _project_rel(fr.file, project_files, base_dir)
            if rel:
                return fr, rel
    for fr in frames:
        if fr.file and not _is_system_path(fr.file):
            return fr, _display_path(fr.file, base_dir)
    return None, None


def _loc_from_frame(fr: StackFrame | None, rel: str | None) -> Location | None:
    if fr is None:
        return None
    return Location(file=rel, line=fr.line, column=fr.column, function=fr.function)


def _loc_from_text(text: str | None, project_files: set[str] | None, base_dir: str | None) -> Location | None:
    """Parse ``file:line[:col][ in func]`` (ASan / UBSan SUMMARY tails)."""
    if not text:
        return None
    m = re.match(r"^(?P<file>\S+?):(?P<line>\d+)(?::(?P<col>\d+))?(?:\s+in\s+(?P<func>\S+))?", text.strip())
    if not m:
        return None
    f = m["file"]
    rel = _project_rel(f, project_files, base_dir) or (None if _is_system_path(f) else _display_path(f, base_dir))
    if rel is None:
        return None
    return Location(file=rel, line=int(m["line"]), column=int(m["col"]) if m["col"] else None, function=m["func"])


def ubsan_rule(message: str) -> str:
    """Rule id for a UBSan ``runtime error:`` message."""
    for pat, rule in _UBSAN_RULES:
        if pat.search(message):
            return rule
    return UBSAN_FALLBACK_RULE


def _asan_kind(rest: str) -> str:
    r = rest.strip()
    r2 = re.sub(r"^attempting\s+", "", r)
    if r2.startswith("free on address which was not malloc"):
        return "bad-free"
    if "exceeds maximum supported size" in r:
        return "allocation-size-too-big"
    if "out of memory" in r:
        return "out-of-memory"
    m = re.match(r"[A-Za-z][\w-]*", r2)
    return m.group(0) if m else "asan-error"


def _clean(text: str, base_dir: str | None) -> str:
    """Drop run-specific addresses / absolute snapshot prefixes from messages
    (the raw excerpt keeps them)."""
    s = _ADDR_PAREN_RE.sub("", text)
    s = _ADDR_RE.sub("<addr>", s)
    if base_dir:
        s = s.replace(base_dir.rstrip("/") + "/", "")
    return s.strip()


def _excerpt(lines: list[str], blk: _Block) -> str:
    end = blk.end if blk.end is not None else len(lines) - 1
    if blk.cut is not None:
        end = min(end, blk.cut - 1)
    chunk = lines[blk.start : end + 1][:_RAW_EXCERPT_LINES]
    text = "\n".join(chunk)
    if len(text) > _RAW_EXCERPT_CHARS:
        text = text[:_RAW_EXCERPT_CHARS] + "\n[... truncated ...]"
    return text


def _scan_blocks(lines: list[str]) -> list[_Block]:
    blocks: list[_Block] = []
    cur: _Block | None = None

    def open_block(b: _Block) -> _Block:
        nonlocal cur
        if cur is not None and cur.end is None:
            cur.end = b.start - 1
        blocks.append(b)
        cur = b
        return b

    for i, raw in enumerate(lines):
        line = raw.rstrip("\r")
        if m := _ASAN_HDR_RE.match(line):
            open_block(_Block("asan", i, header=m["rest"], kind=_asan_kind(m["rest"])))
            continue
        if m := _LSAN_HDR_RE.match(line):
            open_block(_Block("lsan", i, header=m["rest"], kind="memory-leak"))
            continue
        if m := _RSS_HDR_RE.match(line):
            open_block(_Block(_TOOL_NAMES[m["tool"]], i, header=f"hard rss limit exhausted {m['rest']}".strip(),
                              kind="rss-limit-exceeded"))
            continue
        m = _UBSAN_RE.match(line)
        if m:
            open_block(_Block("ubsan", i, ub_file=m["file"], ub_line=int(m["line"]), ub_col=int(m["col"]),
                              ub_msg=m["msg"]))
            continue
        m = _UBSAN_NOLOC_RE.match(line)
        if m:
            f = m["file"]
            open_block(_Block("ubsan", i, ub_file=None if f.startswith(("<", "(")) else f, ub_msg=m["msg"]))
            continue
        if m := _SUMMARY_RE.match(line):
            tool = _TOOL_NAMES[m["tool"]]
            is_leak_summary = "leaked in" in line
            if cur is not None and not cur.closed and (
                cur.tool == tool or (cur.tool == "lsan" and is_leak_summary)
            ):
                if cur.tool != "lsan":
                    cur.summary_kind, cur.summary_rest = m["kind"], m["rest"]
                cur.end = i
                cur.closed = True
                continue
            if is_leak_summary:
                # A leak summary without its report (e.g. truncated output).
                b = _Block("lsan", i, header=line.split(": ", 2)[-1], kind="memory-leak")
            else:
                b = _Block(tool, i, header=line, kind=m["kind"], summary_kind=m["kind"], summary_rest=m["rest"])
            open_block(b)
            b.end, b.closed = i, True
            continue
        if cur is None or cur.closed:
            continue
        if _CUT_RE.match(line):
            if cur.cut is None:
                cur.cut = i
            continue
        if m := _LEAK_RE.match(line):
            cur.leaks.append({"kind": m["kind"], "bytes": int(m["bytes"]), "objects": int(m["objs"]), "frames": []})
            continue
        fr = _parse_frame(line)
        if fr is not None:
            if cur.tool == "lsan" and cur.leaks:
                cur.leaks[-1]["frames"].append(fr)
            else:
                if fr.index == 0 or not cur.stacks:
                    cur.stacks.append([])
                cur.stacks[-1].append(fr)
            continue
        if cur.tool == "asan":
            if (m := _ACCESS_RE.match(line)) and cur.access is None:
                cur.access = f"{m['acc']} of size {m['size']}"
            elif (m := _LOCATED_RE.search(line)) and cur.located is None:
                cur.located = m["what"]
            elif (m := _SIGCAUSE_RE.search(line)) and cur.sigcause is None:
                cur.sigcause = f"{m['acc']} memory access"
            elif (m := _HINT_RE.match(line)) and cur.hint is None:
                cur.hint = m["hint"]
            elif (m := _VAR_RE.search(line)) and cur.variable is None:
                cur.variable = f"{m['what']} '{m['var']}'"
    if cur is not None and cur.end is None:
        cur.end = len(lines) - 1
    return blocks


def parse_sanitizer_findings(
    text: str,
    *,
    project_files: set[str] | None = None,
    base_dir: str | os.PathLike[str] | None = None,
) -> list[SanitizerFinding]:
    """Like :func:`parse_sanitizer_output` but keeps each report's stack."""
    base = str(base_dir) if base_dir is not None else None
    pf = {posixpath.normpath(p) for p in project_files} if project_files is not None else None
    lines = (text or "").splitlines()
    out: list[SanitizerFinding] = []
    seen: set[tuple] = set()

    def add(f: SanitizerFinding) -> None:
        d = f.diagnostic
        key = (d.tool, d.rule_id, d.message, d.location.short() if d.location else None)
        if key in seen:
            return
        seen.add(key)
        out.append(f)

    for blk in _scan_blocks(lines):
        raw = _excerpt(lines, blk)
        if blk.tool == "ubsan":
            add(_ubsan_finding(blk, raw, pf, base))
        elif blk.tool == "lsan" and blk.kind == "memory-leak":
            for f in _lsan_findings(blk, raw, pf, base):
                add(f)
        else:
            add(_asan_finding(blk, raw, pf, base))
    return out


def parse_sanitizer_output(
    text: str,
    *,
    project_files: set[str] | None = None,
    base_dir: str | os.PathLike[str] | None = None,
) -> list[Diagnostic]:
    """Parse ASan / UBSan / LSan reports from a program's stderr.

    * ASan ``==PID==ERROR: AddressSanitizer: <kind> ...`` -> ``tool="asan"``,
      ``rule_id`` = bug type (``heap-buffer-overflow``, ``heap-use-after-free``,
      ``double-free``, ``SEGV``, ...; the ``SUMMARY:`` kind wins when present).
    * UBSan ``file:line:col: runtime error: <msg>`` -> ``tool="ubsan"``,
      ``rule_id`` from the message (``signed-integer-overflow``, ``shift``, ...;
      fallback ``ubsan-runtime-error``).
    * LSan ``ERROR: LeakSanitizer: detected memory leaks`` -> one
      ``tool="lsan"``, ``rule_id="memory-leak"`` diagnostic per leak record.

    The location is the first stack frame in a project file (``project_files``:
    workspace-relative paths; absolute frame paths are matched under
    ``base_dir`` or by suffix), otherwise the first frame with a non-system
    path.  ``raw`` holds an excerpt of the report.
    """
    return [f.diagnostic for f in parse_sanitizer_findings(text, project_files=project_files, base_dir=base_dir)]


def _ubsan_finding(blk: _Block, raw: str, pf: set[str] | None, base: str | None) -> SanitizerFinding:
    frames = blk.stacks[0] if blk.stacks else []
    rule = ubsan_rule(blk.ub_msg) if blk.ub_msg else UBSAN_FALLBACK_RULE
    loc: Location | None = None
    if blk.ub_file:
        rel = _project_rel(blk.ub_file, pf, base)
        if rel is None and not _is_system_path(blk.ub_file):
            rel = _display_path(blk.ub_file, base)
        if rel is not None:
            func = None
            for fr in frames:  # frame #0 is the check site; it may lack line info
                if fr.file and fr.line in (None, blk.ub_line) and (
                    fr.file == blk.ub_file
                    or fr.file.endswith("/" + blk.ub_file.lstrip("./"))
                    or _project_rel(fr.file, pf, base) == rel
                ):
                    func = fr.function
                    break
            loc = Location(file=rel, line=blk.ub_line, column=blk.ub_col, function=func)
    if loc is None:
        fr, rel = _pick_frame(frames, pf, base)
        loc = _loc_from_frame(fr, rel) or _loc_from_text(blk.summary_rest, pf, base)
    if blk.ub_msg:
        msg = f"UndefinedBehaviorSanitizer: {_clean(blk.ub_msg, base)}"
    else:
        msg = f"UndefinedBehaviorSanitizer: {blk.summary_kind or 'undefined-behavior'}"
    path = [PathStep(location=Location(file=_project_rel(fr.file, pf, base) or _display_path(fr.file, base),
                                       line=fr.line, column=fr.column, function=fr.function),
                     message=f"frame #{fr.index}")
            for fr in frames if fr.file and not _is_system_path(fr.file)][:_CALL_STACK_MAX]
    d = Diagnostic(tool="ubsan", rule_id=rule, severity="error", message=msg, location=loc, path=path, raw=raw)
    return SanitizerFinding(d, frames, base)


def _asan_finding(blk: _Block, raw: str, pf: set[str] | None, base: str | None) -> SanitizerFinding:
    kind = blk.summary_kind if (blk.summary_kind and blk.kind != "rss-limit-exceeded") else (blk.kind or "asan-error")
    frames = blk.stacks[0] if blk.stacks else []
    fr, rel = _pick_frame(frames, pf, base)
    loc = _loc_from_frame(fr, rel) or _loc_from_text(blk.summary_rest, pf, base)
    extra = [x for x in (blk.access, f"located {blk.located}" if blk.located else None, blk.sigcause, blk.hint,
                         blk.variable) if x]
    title = _TOOL_TITLES.get(blk.tool, "AddressSanitizer")
    if kind == "rss-limit-exceeded":
        msg = f"{title}: {blk.header}"
    else:
        msg = f"{title}: {kind}"
    if extra:
        msg += " (" + "; ".join(_clean(x, base) for x in extra) + ")"
    path = [PathStep(location=Location(file=_project_rel(f.file, pf, base) or _display_path(f.file, base),
                                       line=f.line, column=f.column, function=f.function),
                     message=f"frame #{f.index}")
            for f in frames if f.file and not _is_system_path(f.file)][:_CALL_STACK_MAX]
    d = Diagnostic(tool=blk.tool, rule_id=kind, severity="error", message=_clean(msg, base), location=loc,
                   path=path, raw=raw)
    return SanitizerFinding(d, frames, base)


def _lsan_findings(blk: _Block, raw: str, pf: set[str] | None, base: str | None) -> list[SanitizerFinding]:
    if not blk.leaks:
        d = Diagnostic(tool="lsan", rule_id="memory-leak", severity="error",
                       message=f"LeakSanitizer: {_clean(blk.header, base)}", raw=raw)
        return [SanitizerFinding(d, [], base)]
    out = []
    for leak in blk.leaks:
        frames: list[StackFrame] = leak["frames"]
        fr, rel = _pick_frame(frames, pf, base)
        loc = _loc_from_frame(fr, rel)
        msg = f"LeakSanitizer: {leak['kind']} leak of {leak['bytes']} byte(s) in {leak['objects']} object(s)"
        if loc is not None:
            msg += f" allocated from {loc.short()}"
        d = Diagnostic(tool="lsan", rule_id="memory-leak", severity="error", message=msg, location=loc, raw=raw)
        out.append(SanitizerFinding(d, frames, base))
    return out


# =========================================================================== build diagnostics
# A deliberately small, tolerant parser (the full one lives in the compile
# gate).  It only needs to explain why the sanitizer build failed.

_SEV = r"fatal error|error|warning|note"
_CC_DIAG_RE = re.compile(rf"^(?P<file>[^\s:][^:]*?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<sev>{_SEV}):\s*(?P<msg>.*?)\s*$")
_CC_OPT_RE = re.compile(r"^(?P<msg>.*?)\s*\[(?P<opt>-W[^\[\]]*)\]$")
_LD_PROG = r"(?:\S*/)?(?:ld|ld\.[A-Za-z0-9_]+|lld|collect2)"
_LD_FUNC_RE = re.compile(rf"^(?:{_LD_PROG}:\s+)?(?P<obj>.+?): in function [`'‘](?P<func>[^'`’]+)['’]:\s*$")
_LD_REF_RE = re.compile(
    rf"^(?:{_LD_PROG}:\s+)?(?P<where>.*?):\s*(?P<msg>(?:undefined reference to|multiple definition of)\s.*?)\s*$"
)
_LD_WHERE_RE = re.compile(r"^(?P<file>[^:(]+?)(?::(?P<line>\d+))?:\(")
_DRIVER_RE = re.compile(
    rf"^(?P<prog>(?:\S*/)?clang(?:\+\+)?(?:-\d+)?|{_LD_PROG}):\s*(?:(?P<sev>fatal error|error|warning):\s*)?(?P<msg>.+?)\s*$"
)
_CRASH_RE = re.compile(
    r"PLEASE submit a bug report|clang frontend command failed due to signal|unable to execute command|^Stack dump:",
    re.MULTILINE,
)


def _rel_build_path(path: str, base_dir: str | None) -> str | None:
    p = path.strip()
    if base_dir and p.startswith(base_dir.rstrip("/") + "/"):
        return posixpath.normpath(p[len(base_dir.rstrip("/")) + 1 :])
    if p.endswith(".o") and (p.startswith("/tmp/") or "/" not in p):
        return None  # clang's temporary object file
    return p


def parse_build_diagnostics(text: str, *, base_dir: str | None = None) -> list[Diagnostic]:
    """Compiler / linker messages of the sanitizer build as ``tool="clang"``
    diagnostics (notes are attached to the preceding diagnostic as path steps)."""
    out: list[Diagnostic] = []
    func_ctx: str | None = None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if m := _LD_FUNC_RE.match(line):
            func_ctx = m["func"]
            continue
        if m := _LD_REF_RE.match(line):
            where = m["where"].strip()
            wm = _LD_WHERE_RE.match(where)
            f = _rel_build_path(wm["file"] if wm else where, base_dir) if where else None
            ln = int(wm["line"]) if wm and wm["line"] else None
            loc = Location(file=f, line=ln, function=func_ctx) if (f or func_ctx) else None
            rule = "undefined-reference" if m["msg"].startswith("undefined") else "multiple-definition"
            out.append(Diagnostic(tool="clang", rule_id=rule, severity="error", message=m["msg"], location=loc,
                                  raw=line))
            continue
        if m := _CC_DIAG_RE.match(line):
            sev = "error" if m["sev"] in ("error", "fatal error") else m["sev"]
            msg, rule = m["msg"], None
            if om := _CC_OPT_RE.match(msg):
                msg, rule = om["msg"], om["opt"].split(",")[-1]
            loc = Location(file=_rel_build_path(m["file"], base_dir), line=int(m["line"]),
                           column=int(m["col"]) if m["col"] else None)
            if sev == "note" and out:
                out[-1].path.append(PathStep(location=loc, message=msg))
                out[-1].raw = (out[-1].raw or "") + "\n" + line
                continue
            out.append(Diagnostic(tool="clang", rule_id=rule, severity=sev, message=msg, location=loc, raw=line))
            continue
        if m := _DRIVER_RE.match(line):
            sev = m["sev"] or "error"
            sev = "error" if sev in ("error", "fatal error") else sev
            msg = m["msg"]
            rule = "link-error" if "linker command failed" in msg or "ld" in m["prog"].rsplit("/", 1)[-1] else "driver"
            out.append(Diagnostic(tool="clang", rule_id=rule, severity=sev, message=msg, raw=line))
            continue
    return out


# =========================================================================== build configuration


@dataclass(frozen=True)
class SanitizerPlan:
    """How the requested sanitizers map onto clang flags and runtime options."""

    requested: tuple[str, ...]
    effective: tuple[str, ...]  # sanitizers actually active at run time
    fsanitize: tuple[str, ...]  # values for -fsanitize=
    detect_leaks: bool
    notes: tuple[str, ...] = ()

    @property
    def compile_flags(self) -> list[str]:
        if not self.fsanitize:
            return []
        return ["-fno-sanitize-recover=all", "-fsanitize=" + ",".join(self.fsanitize)]

    @property
    def uses_shadow_runtime(self) -> bool:
        """ASan / LSan reserve huge virtual ranges: RLIMIT_AS must not be set."""
        return "address" in self.fsanitize or "leak" in self.fsanitize


def sanitizer_plan(requested: Iterable[str]) -> SanitizerPlan:
    """Map ``tests.sanitizers`` to ``-fsanitize=`` values.

    * ``leak`` is part of ASan on Linux (enabled with ``detect_leaks=1``);
    * ``leak`` without ``address`` uses the standalone LSan runtime;
    * ``leak`` + ``undefined`` without ``address`` cannot be linked with clang's
      runtimes (the standalone LSan runtime lacks the UBSan handlers), so
      ``address`` is added -- strictly more checking, recorded as a note.
    """
    req = tuple(dict.fromkeys(requested))
    notes: list[str] = []
    want_a, want_u, want_l = "address" in req, "undefined" in req, "leak" in req
    fs: list[str] = []
    if want_l and want_u and not want_a:
        want_a = True
        notes.append(
            "leak+undefined without address cannot be linked (standalone LSan runtime lacks UBSan handlers); "
            "using address,undefined (ASan includes LeakSanitizer)"
        )
    if want_a:
        fs.append("address")
    if want_u:
        fs.append("undefined")
    if want_l and not want_a:
        fs.append("leak")
    effective = tuple(s for s in ("address", "undefined", "leak") if (s in fs) or (s == "leak" and want_l))
    return SanitizerPlan(requested=req, effective=effective, fsanitize=tuple(fs), detect_leaks=want_l,
                         notes=tuple(notes))


def sanitizer_env(plan: SanitizerPlan, memory_mb: int, symbolizer: str | None) -> dict[str, str]:
    """Runtime options for the sanitized test binary."""
    asan = [
        f"detect_leaks={1 if plan.detect_leaks else 0}",
        "halt_on_error=1",
        "abort_on_error=0",
        f"exitcode={EXIT_ASAN}",
        f"hard_rss_limit_mb={int(memory_mb)}",
        "allocator_may_return_null=1",
        "detect_stack_use_after_return=1",
        "symbolize=1",
    ]
    env = {
        "ASAN_OPTIONS": ":".join(asan),
        "UBSAN_OPTIONS": f"print_stacktrace=1:halt_on_error=1:exitcode={EXIT_UBSAN}",
        "LSAN_OPTIONS": f"exitcode={EXIT_LSAN}:hard_rss_limit_mb={int(memory_mb)}",
    }
    if symbolizer:
        for var in ("ASAN_SYMBOLIZER_PATH", "UBSAN_SYMBOLIZER_PATH", "LSAN_SYMBOLIZER_PATH"):
            env[var] = symbolizer
    return env


def find_llvm_symbolizer(clang: str | None = None, clang_version: str | None = None) -> str | None:
    """An ``llvm-symbolizer`` under ``/usr`` (so it is visible inside the
    sandbox), preferring the one shipped next to ``clang``."""
    cands: list[str] = []
    if clang:
        cands.append(os.path.join(os.path.dirname(os.path.realpath(clang)), "llvm-symbolizer"))
    m = re.search(r"clang version (\d+)", clang_version or "")
    if m:
        cands += [f"/usr/lib/llvm-{m.group(1)}/bin/llvm-symbolizer", f"/usr/bin/llvm-symbolizer-{m.group(1)}"]
    cands.append("/usr/bin/llvm-symbolizer")
    cands += sorted(glob.glob("/usr/lib/llvm-*/bin/llvm-symbolizer"), reverse=True)
    for c in cands:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            real = os.path.realpath(c)
            if real.startswith("/usr/"):
                return real
    return None


def build_command(ctx: BackendContext, binary: Path, plan: SanitizerPlan) -> list[str]:
    """argv building the sanitized test binary (cwd = snapshot, relative sources)."""
    spec = ctx.spec
    assert compiler(ctx) and spec.tests.driver
    defines = [d if d.startswith("-D") else f"-D{d}" for d in spec.compile.defines]
    return [
        compiler(ctx),  # clang++ for C++ specs (also links the C++ and UBSan C++ runtimes)
        f"-std={spec.language.standard}",
        *BASE_FLAGS,
        *plan.compile_flags,
        *spec.compile.flags,
        *defines,
        *ctx.include_args(),
        f"-I{os.path.realpath(ctx.include_dir)}",
        *DIAGNOSTIC_FLAGS,
        *spec.sources.editable,
        spec.tests.driver,
        "-o",
        str(binary),
    ]


# =========================================================================== sanitizer-control scan

_SAN_CONTROL_RE = re.compile(
    r"\b__(?:asan|lsan|ubsan|msan|tsan|hwasan|dfsan|sanitizer|sancov)_\w+"
    r"|\bno_sanitize\w*"
    r"|\bdisable_sanitizer_instrumentation\b"
    r"|\b__SANITIZE_\w+__"
    r"|\b__has_feature\s*\(\s*\w*sanitizer\w*\s*\)"
    r"|#\s*include\s*[<\"]\s*sanitizer/"
)


def _strip_comments(src: str) -> str:
    """Blank out C comments (keeping newlines and string/char literals) so the
    scan neither flags commentary nor can be hidden behind ``"/*"`` tricks."""
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in src[i:j]))
            i = j
        elif c in "\"'":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(src[i:j])
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def scan_sanitizer_control(snapshot: Path, editable: Iterable[str]) -> list[Diagnostic]:
    """Find constructs in *editable* sources that could switch sanitizer
    checks off or detect that they are on."""
    out: list[Diagnostic] = []
    for rel in editable:
        p = snapshot / rel
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(_strip_comments(text).splitlines(), 1):
            for m in _SAN_CONTROL_RE.finditer(line):
                out.append(
                    Diagnostic(
                        tool="leanc-policy",
                        rule_id="sanitizer-control",
                        severity="error",
                        message=(
                            f"editable source uses {m.group(0).strip()!r}: code under test must not configure, "
                            "suppress or detect the sanitizers that judge it"
                        ),
                        location=Location(file=rel, line=lineno, column=m.start() + 1),
                        raw=line.strip()[:300],
                    )
                )
    return out


# =========================================================================== running


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:60] or "case"


def _unique(base: str, seen: set[str]) -> str:
    cand, k = base, 2
    while cand in seen:
        cand = f"{base}#{k}"
        k += 1
    seen.add(cand)
    return cand


def _signame(sig: int) -> str:
    try:
        return _signal.Signals(sig).name
    except ValueError:
        return f"signal {sig}"


def _effective_signal(proc: ProcResult) -> int | None:
    """Signal that terminated the program.  bubblewrap reports a child killed
    by signal N as exit status 128+N."""
    if proc.signal is not None:
        return proc.signal
    if proc.sandbox != "none" and proc.exit_code is not None and 128 < proc.exit_code < 128 + 65:
        return proc.exit_code - 128
    return None


def _first_diff(expected: str, got: str) -> str:
    el, gl = expected.splitlines(), got.splitlines()
    for i in range(max(len(el), len(gl))):
        e = el[i] if i < len(el) else None
        g = gl[i] if i < len(gl) else None
        if e != g:
            return f"first difference at line {i + 1}: expected {e!r:.120}, got {g!r:.120}"
    return f"outputs differ only in line endings / trailing newline (expected {len(expected)} chars, got {len(got)})"


def _looks_like_sandbox_failure(proc: ProcResult) -> bool:
    if proc.sandbox == "none" or proc.exit_code != 1 or proc.stdout:
        return False
    lines = [ln for ln in proc.stderr.splitlines() if ln.strip()]
    return 0 < len(lines) <= 3 and lines[0].startswith("bwrap: ")


def _rel(ctx: BackendContext, p: Path) -> str:
    """Path relative to the run directory (both canonicalised: the sanitizer
    stage works with real paths because the sandbox binds real paths)."""
    return os.path.relpath(os.path.realpath(p), os.path.realpath(ctx.run_dir))


def _judge_case(
    *,
    case: TestCase,
    proc: ProcResult,
    sig: int | None,
    cpu_limited: bool,
    findings: list[SanitizerFinding],
    inputs: list[CounterexampleInput],
    raw_ref: str,
    timeout_s: float,
    max_output_bytes: int,
    entry: dict,
    seen_ids: set[str],
) -> tuple[str, list[Diagnostic], list[FailureReport], str | None]:
    """Judge one executed test case.

    Returns ``(status, diagnostics, failures, error)`` with status one of
    ``pass | fail | timeout | error``.  Precedence: sanitizer findings and
    oracle mismatches (definite evidence) > tool problems > timeout.
    """
    diags: list[Diagnostic] = []
    fails: list[FailureReport] = []
    timed_out = bool(entry["timed_out"])
    argv_text = shlex.join(case.args)

    def oracle(rule: str, message: str, explanation: str, kind: str = "test-oracle") -> None:
        text = f"test case {case.name!r}: {message}"
        diags.append(Diagnostic(tool="test-oracle", rule_id=rule, severity="error", message=text))
        fails.append(
            FailureReport(
                id=_unique(f"sanitizer:{case.name}:test-oracle:{rule}", seen_ids),
                kind=kind,  # type: ignore[arg-type]
                tool="test-oracle",
                property_id=rule,
                description=text,
                inputs=list(inputs),
                explanation=explanation,
                raw_ref=raw_ref,
            )
        )

    error: str | None = None
    if proc.error:
        error = f"{case.name}: could not run the test binary: {proc.error}"
    elif _looks_like_sandbox_failure(proc) and not findings:
        error = f"{case.name}: sandbox failed to start the test: {proc.stderr.strip()[:300]}"
    elif not findings and _INTERNAL_ERROR_RE.search(proc.stderr):
        line = next(ln for ln in proc.stderr.splitlines() if _INTERNAL_ERROR_RE.search(ln))
        error = f"{case.name}: sanitizer runtime failure: {line.strip()[:300]}"

    for f in findings:
        d = f.diagnostic
        d.message = f"{d.message} [test case {case.name!r}]"
        diags.append(d)
        where = f" at {d.location.short()}" if d.location else ""
        entry["findings"].append(
            {"tool": d.tool, "rule_id": d.rule_id, "location": d.location.short() if d.location else None}
        )
        fails.append(
            FailureReport(
                id=_unique(f"sanitizer:{case.name}:{d.tool}:{d.rule_id}", seen_ids),
                kind="runtime",
                tool=d.tool,
                property_id=d.rule_id,
                description=d.message,
                location=d.location,
                inputs=list(inputs),
                call_stack=f.call_stack(),
                explanation=(
                    f"{_TOOL_TITLES.get(d.tool, d.tool)} reported {d.rule_id}{where} while executing trusted test "
                    f"case {case.name!r} (argv: {argv_text or '<none>'}). A sanitizer report is definite "
                    "evidence of memory-unsafe or undefined behaviour on this executed path."
                ),
                raw_ref=raw_ref,
            )
        )

    if error is not None:
        return ("fail" if findings else "error"), diags, fails, error

    oracle_failed = False
    if proc.output_truncated:
        oracle_failed = True
        oracle(
            "output-limit-exceeded",
            f"output exceeded the limit of {max_output_bytes} bytes per stream; the process was killed",
            "The program printed more than tests.max_output_bytes (e.g. a runaway loop); its exit status "
            "and output were not checked.",
        )
    elif timed_out:
        why = "CPU time limit" if cpu_limited and not proc.timed_out else "wall-clock timeout"
        entry["timeout_reason"] = why
        if not findings:
            oracle(
                "timeout",
                f"did not finish within {timeout_s:g}s ({why}); the process group was killed",
                "Non-termination (or extreme slowness) on this input; no verdict about its output.",
                kind="timeout",
            )
    elif not findings:
        # With a sanitizer finding the program died inside the sanitizer
        # runtime (even LeakSanitizer leaves through _exit() from its atexit
        # hook, so buffered stdout is lost): exit status and output are then
        # consequences of the finding, not independent evidence.
        if proc.exit_code != case.expect_exit:
            oracle_failed = True
            got = f"exit code {proc.exit_code}" if proc.exit_code is not None else "no exit code"
            extra = ""
            if sig is not None:
                extra = f" (terminated by {_signame(sig)})"
            elif proc.exit_code in SANITIZER_EXIT_CODES:
                extra = (
                    f" (this is the exit code Lean C configures for {SANITIZER_EXIT_CODES[proc.exit_code]}, "
                    "but no sanitizer report was found on stderr)"
                )
            oracle(
                "exit-code",
                f"{got}{extra}, expected {case.expect_exit}",
                "The trusted test driver signals a failed check through its exit status; see the "
                "case's stderr for the failing check.",
            )
        if case.expect_stdout is not None:
            matches = proc.stdout == case.expect_stdout
            entry["stdout_matches"] = matches
            if not matches:
                oracle_failed = True
                oracle(
                    "stdout",
                    f"stdout differs from the expected output ({_first_diff(case.expect_stdout, proc.stdout)})",
                    "The program's standard output must equal tests.cases[].expect_stdout exactly.",
                )

    if findings or oracle_failed:
        return "fail", diags, fails, None
    if timed_out:
        return "timeout", diags, fails, None
    return "pass", diags, fails, None


def run_sanitizer_tests(ctx: BackendContext) -> StageResult:
    """Build the sanitized test binary and run every trusted test case.

    Outcome: any sanitizer finding / oracle mismatch / build failure -> FAIL
    (``RUNTIME_TEST_FAILED``; a build or link failure is reported as
    ``COMPILE_ERROR``); otherwise any timeout -> TIMEOUT; otherwise a tool
    problem (sanitizer runtime failure, launch failure) -> ERROR; no permission
    to execute untrusted code -> SKIPPED; otherwise PASS.
    """
    t0 = time.monotonic()
    spec = ctx.spec
    tests = spec.tests
    plan = sanitizer_plan(tests.sanitizers)
    diagnostics: list[Diagnostic] = []
    failures: list[FailureReport] = []
    invocations: list[ToolInvocation] = []
    seen_ids: set[str] = set()
    details: dict = {
        "sanitizers": list(plan.effective),
        "requested_sanitizers": list(plan.requested),
        "fsanitize": list(plan.fsanitize),
        "compiler": compiler(ctx),
        "compiler_version": compiler_version(ctx),
        "sandbox": ctx.sandbox.kind,
        "limitation": LIMITATION,
        "cases": [],
    }
    if plan.notes:
        details["sanitizer_notes"] = list(plan.notes)

    def result(outcome: Outcome, summary: str, status: Status | None | str = "auto") -> StageResult:
        return StageResult(
            stage=STAGE,
            outcome=outcome,
            status=outcome_to_status(STAGE, outcome) if status == "auto" else status,  # type: ignore[arg-type]
            summary=summary,
            diagnostics=diagnostics,
            failures=failures,
            invocations=invocations,
            details=details,
            duration_s=round(time.monotonic() - t0, 3),
        )

    if not tests.driver or not tests.cases:
        return result(Outcome.SKIPPED, "no trusted test driver / test cases configured (tests.driver, tests.cases)")
    if not compiler(ctx):
        return result(
            Outcome.ERROR,
            compiler_missing(spec, f"C compiler not found (spec.compile.compiler={spec.compile.compiler!r}); install clang or set LEANC_CLANG"),
        )

    snap = Path(os.path.realpath(ctx.snapshot_dir))
    out_root = Path(os.path.realpath(ctx.build_dir)) / "sanitizer"
    raw_root = Path(os.path.realpath(ctx.raw_dir)) / "sanitizer"
    out_root.mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)
    project_files = {*spec.sources.editable, *spec.sources.headers, tests.driver}

    if not (snap / tests.driver).is_file():
        return result(
            Outcome.ERROR,
            f"trusted test driver {tests.driver!r} is missing from the verification snapshot",
        )
    missing = [rel for rel in spec.sources.editable if not (snap / rel).is_file()]
    if missing:
        for rel in missing:
            msg = f"editable source {rel!r} is missing from the verification snapshot"
            diagnostics.append(Diagnostic(tool="leanc", rule_id="missing-source", severity="error", message=msg,
                                          location=Location(file=rel)))
            failures.append(FailureReport(id=_unique(f"sanitizer:build:missing-source:{rel}", seen_ids),
                                          kind="compile", tool="leanc", property_id="missing-source",
                                          description=msg, location=Location(file=rel)))
        return result(Outcome.FAIL, f"cannot build the sanitizer test binary: missing {', '.join(missing)}",
                      status=Status.COMPILE_ERROR)

    # ------------------------------------------------------------------ policy scan
    control = scan_sanitizer_control(snap, spec.sources.editable)
    for d in control:
        diagnostics.append(d)
        failures.append(
            FailureReport(
                id=_unique(f"sanitizer:policy:sanitizer-control:{d.location.file if d.location else '?'}", seen_ids),
                kind="policy",
                tool="leanc-policy",
                property_id="sanitizer-control",
                description=d.message,
                location=d.location,
                explanation="Sanitizer verdicts are only meaningful if the code under test cannot influence the "
                "sanitizer runtime; remove this construct.",
            )
        )

    # ------------------------------------------------------------------ build
    binary = out_root / "test_bin"
    if binary.exists():
        binary.unlink()  # never run a stale binary
    argv = build_command(ctx, binary, plan)
    details["build"] = {"argv": list(argv), "cwd": "snapshot", "binary": _rel(ctx, binary)}
    proc = ctx.run(argv, limits=Limits(timeout_s=ctx.timeout(spec.compile.timeout_s)), cwd=snap)
    invocations.append(proc.to_invocation("clang", ctx.run_dir, ctx.log_name(STAGE, "build")))
    build_raw = raw_root / "build.stderr.txt"
    build_raw.write_text(redact(proc.stderr), encoding="utf-8")
    details["build"].update({"exit_code": proc.exit_code, "raw": _rel(ctx, build_raw)})
    if proc.error:
        return result(Outcome.ERROR, f"sanitizer build could not start: {proc.error}")
    if proc.timed_out:
        return result(Outcome.TIMEOUT, f"sanitizer build timed out after {ctx.timeout(spec.compile.timeout_s):g}s")
    build_diags = parse_build_diagnostics(proc.stderr, base_dir=str(snap))
    sig = _effective_signal(proc)
    if sig is not None or _CRASH_RE.search(proc.stderr):
        why = f"terminated by {_signame(sig)}" if sig is not None else "compiler crash reported"
        return result(Outcome.ERROR, f"clang crashed while building the sanitizer test binary ({why})")
    details["build"]["warnings"] = sum(1 for d in build_diags if d.severity == "warning")
    if proc.exit_code != 0 or not binary.is_file():
        diagnostics.extend(build_diags)
        errors = [d for d in build_diags if d.severity == "error"]
        first = errors[0] if errors else None
        desc = (
            "the editable sources do not build/link against the trusted test driver with sanitizers"
            + (f": {first.message}" if first else f" (clang exit code {proc.exit_code})")
        )
        failures.append(
            FailureReport(
                id=_unique("sanitizer:build", seen_ids),
                kind="compile",
                tool="clang",
                property_id=first.rule_id if first else None,
                description=desc,
                location=first.location if first else None,
                explanation="The generated code must provide every symbol the trusted test driver uses "
                "(and nothing that clashes with it, such as a second main()).",
                raw_ref=_rel(ctx, build_raw),
            )
        )
        return result(Outcome.FAIL, f"sanitizer build failed: {desc}", status=Status.COMPILE_ERROR)

    # ------------------------------------------------------------------ permission to execute
    ok, reason = ctx.may_execute_untrusted()
    details["sandbox_reason"] = reason
    if not ok:
        return result(Outcome.SKIPPED, reason)
    if not ctx.sandbox.active:
        details["sandbox_warning"] = reason

    symbolizer = find_llvm_symbolizer(compiler(ctx), compiler_version(ctx))
    details["symbolizer"] = symbolizer
    if not symbolizer:
        details["symbolizer_warning"] = "llvm-symbolizer not found under /usr: stack frames lack file:line"
    env = sanitizer_env(plan, tests.memory_mb, symbolizer)
    details["env"] = dict(env)
    details["exit_codes"] = {
        "asan": EXIT_ASAN,
        "ubsan": EXIT_UBSAN,
        "lsan": EXIT_LSAN,
        "note": "the combined ASan+UBSan runtime shares one exitcode flag (UBSAN_OPTIONS is parsed last); "
        "findings are attributed from the parsed reports, not from exit codes",
    }
    cases_root = out_root / "cases"
    if cases_root.exists():
        shutil.rmtree(cases_root)
    cases_root.mkdir(parents=True)

    n_fail = n_timeout = n_error = 0
    timed_out_names: list[str] = []
    error_msgs: list[str] = []
    for idx, case in enumerate(tests.cases):
        case_dir = cases_root / f"{idx:02d}-{_safe(case.name)}"
        case_dir.mkdir()
        timeout_s = ctx.timeout(case.timeout_s or tests.timeout_s)
        limits = Limits(
            timeout_s=timeout_s,
            cpu_s=math.ceil(timeout_s),
            memory_mb=None if plan.uses_shadow_runtime else tests.memory_mb,
            max_output_bytes=tests.max_output_bytes,
            max_file_mb=64,  # untrusted code: cap every file it may write
        )
        mounts = SandboxMounts(ro=[str(out_root)], rw=[str(case_dir)])
        stdin_data = case.stdin.encode("utf-8") if case.stdin is not None else None
        run_argv = [str(binary), *case.args]
        proc = ctx.run(run_argv, limits=limits, cwd=case_dir, env_extra=env, stdin_data=stdin_data, mounts=mounts)
        invocations.append(proc.to_invocation("sanitizer-test", ctx.run_dir, ctx.log_name(STAGE, case.name)))
        err_raw = raw_root / f"{idx:02d}-{_safe(case.name)}.stderr.txt"
        out_raw = raw_root / f"{idx:02d}-{_safe(case.name)}.stdout.txt"
        err_raw.write_text(redact(proc.stderr), encoding="utf-8")
        out_raw.write_text(redact(proc.stdout), encoding="utf-8")
        raw_ref = _rel(ctx, err_raw)

        sig = _effective_signal(proc)
        cpu_limited = sig == _signal.SIGXCPU
        timed_out = proc.timed_out or cpu_limited
        findings = parse_sanitizer_findings(proc.stderr, project_files=project_files, base_dir=str(snap))
        argv_text = shlex.join(case.args)
        inputs = [
            CounterexampleInput(name="test_case", value=case.name),
            CounterexampleInput(name="argv", value=argv_text),
        ]
        if case.stdin is not None:
            inputs.append(CounterexampleInput(name="stdin", value=case.stdin[:2000]))
        entry: dict = {
            "name": case.name,
            "args": list(case.args),
            "exit_code": proc.exit_code,
            "signal": sig,
            "expected_exit": case.expect_exit,
            "timed_out": timed_out,
            "output_truncated": proc.output_truncated,
            "findings": [],
            "stdout_matches": None,
            "duration_s": round(proc.duration_s, 3),
            "timeout_s": timeout_s,
            "cwd": _rel(ctx, case_dir),
            "raw_stderr": raw_ref,
            "raw_stdout": _rel(ctx, out_raw),
        }
        details["cases"].append(entry)
        status, case_diags, case_failures, case_error = _judge_case(
            case=case,
            proc=proc,
            sig=sig,
            cpu_limited=cpu_limited,
            findings=findings,
            inputs=inputs,
            raw_ref=raw_ref,
            timeout_s=timeout_s,
            max_output_bytes=tests.max_output_bytes,
            entry=entry,
            seen_ids=seen_ids,
        )
        diagnostics.extend(case_diags)
        failures.extend(case_failures)
        entry["status"] = status
        if status == "fail":
            n_fail += 1
        elif status == "error":
            entry["error"] = case_error
            n_error += 1
            error_msgs.append(case_error or case.name)
        elif status == "timeout":
            n_timeout += 1
            timed_out_names.append(case.name)

    total = len(tests.cases)
    details["counts"] = {
        "cases": total,
        "passed": total - n_fail - n_timeout - n_error,
        "failed": n_fail,
        "timed_out": n_timeout,
        "errors": n_error,
        "findings": sum(len(c["findings"]) for c in details["cases"]),
    }
    san = "+".join(plan.effective) or "no"
    where = f"sandbox: {ctx.sandbox.kind}" if ctx.sandbox.active else "UNSANDBOXED"
    if control or n_fail:
        parts = []
        if n_fail:
            descr = []
            for c in details["cases"]:
                if c["status"] != "fail":
                    continue
                if c["findings"]:
                    f0 = c["findings"][0]
                    descr.append(f"{c['name']} ({f0['tool']} {f0['rule_id']}" +
                                 (f" at {f0['location']}" if f0["location"] else "") + ")")
                else:
                    descr.append(f"{c['name']} (test oracle)")
            parts.append(f"{n_fail} of {total} test case(s) failed: " + ", ".join(descr))
        if control:
            parts.append(f"{len(control)} sanitizer-control construct(s) in editable sources")
        return result(Outcome.FAIL, "; ".join(parts))
    if n_timeout:
        return result(Outcome.TIMEOUT, f"test case(s) timed out: {', '.join(timed_out_names)}")
    if n_error:
        return result(Outcome.ERROR, "; ".join(error_msgs))
    return result(Outcome.PASS, f"{total}/{total} test case(s) passed under {san} sanitizer(s) ({where})")
