from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass, field
from typing import Literal

from leanc.backends.sanitizer.frames import (
    CALL_STACK_MAX_FRAMES,
    REPORT_NUMBER,
    StackFrame,
    display_path,
    frame_path_steps,
    location_from_frame,
    location_from_summary_tail,
    parse_frame,
    pick_frame,
    project_relative_path,
    reportable_path,
)
from leanc.foundation.models import Diagnostic, Location

SanitizerTool = Literal["asan", "lsan", "ubsan"]

TOOL_BY_RUNTIME_NAME: dict[str, SanitizerTool] = {
    "AddressSanitizer": "asan",
    "LeakSanitizer": "lsan",
    "UndefinedBehaviorSanitizer": "ubsan",
}
TOOL_TITLES = {tool: title for title, tool in TOOL_BY_RUNTIME_NAME.items()}

UBSAN_FALLBACK_RULE = "ubsan-runtime-error"
ASAN_FALLBACK_RULE = "asan-error"
MEMORY_LEAK_RULE = "memory-leak"
RSS_LIMIT_RULE = "rss-limit-exceeded"
UNDEFINED_BEHAVIOR_KIND = "undefined-behavior"
LEAK_SUMMARY_MARKER = "leaked in"

RAW_EXCERPT_MAX_LINES = 60
RAW_EXCERPT_MAX_CHARS = 6000
EXCERPT_TRUNCATION_MARKER = "\n[... truncated ...]"

_ASAN_HEADER_RE = re.compile(r"^==\d+==\s*ERROR: AddressSanitizer: (?P<rest>.+?)\s*$")
_LSAN_HEADER_RE = re.compile(r"^==\d+==\s*ERROR: LeakSanitizer: (?P<rest>.+?)\s*$")
_RSS_LIMIT_HEADER_RE = re.compile(
    r"^==\d+==\s*(?P<tool>AddressSanitizer|LeakSanitizer|UndefinedBehaviorSanitizer): "
    r"hard rss limit exhausted\s*(?P<rest>.*?)\s*$"
)
_UBSAN_LOCATED_RE = re.compile(
    rf"^(?P<file>.+?):(?P<line>{REPORT_NUMBER}):(?P<col>{REPORT_NUMBER}): runtime error: (?P<msg>.*?)\s*$"
)
_UBSAN_UNLOCATED_RE = re.compile(
    r"^(?P<file><unknown>|\S+\.(?:c|h|cc|cpp|cxx|hpp|inc)|\([^()]*\+0x[0-9a-fA-F]+\)): runtime error: (?P<msg>.*?)\s*$"
)
_SUMMARY_RE = re.compile(
    r"^SUMMARY: (?P<tool>AddressSanitizer|LeakSanitizer|UndefinedBehaviorSanitizer): "
    r"(?P<kind>\S+)(?:\s+(?P<rest>.*?))?\s*$"
)
_LEAK_RECORD_RE = re.compile(
    rf"^(?P<kind>Direct|Indirect) leak of (?P<bytes>{REPORT_NUMBER}) byte\(s\) in (?P<objs>{REPORT_NUMBER}) object\(s\) allocated from:"
)
_ACCESS_RE = re.compile(r"^(?P<acc>READ|WRITE) of size (?P<size>\d+) at ")
_LOCATED_RE = re.compile(r"\bis located (?P<what>.+?)\s*$")
_SIGNAL_CAUSE_RE = re.compile(r"The signal is caused by a (?P<acc>\w+) memory access")
_HINT_RE = re.compile(r"^==\d+==\s*Hint: (?P<hint>.+?)\s*$")
_VARIABLE_RE = re.compile(r"\[\d+, \d+\) '(?P<var>[^']+)'.*<== (?P<what>Memory access .+?)\s*$")
_EXCERPT_END_RE = re.compile(r"^(?:Shadow bytes around the buggy address|==\d+==\s*Process memory map follows)")
_ADDRESS_GROUP_RE = re.compile(r"\s*\(0x[0-9a-fA-F]+\)|\s*\[0x[0-9a-fA-F]+,\s*0x[0-9a-fA-F]+\)")
_ADDRESS_RE = re.compile(r"\b0x[0-9a-fA-F]{5,}\b")
_ASAN_ATTEMPTING_RE = re.compile(r"^attempting\s+")
_ASAN_KIND_WORD_RE = re.compile(r"[A-Za-z][\w-]*")

_RUNTIME_INTERNAL_ERROR_RE = re.compile(
    r"LeakSanitizer has encountered a fatal error"
    r"|Sanitizer CHECK failed"
    r"|AddressSanitizer: CHECK failed"
    r"|ReserveShadowMemoryRange failed"
    r"|Shadow memory range interleaves"
    r"|ERROR: AddressSanitizer failed to allocate"
    r"|ASan runtime does not come first"
    r"|ERROR: Failed to mmap"
)

_UBSAN_RULES_FIRST_MATCH_WINS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), rule)
    for pattern, rule in (
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
_ASAN_KIND_BY_PHRASE = (
    ("exceeds maximum supported size", "allocation-size-too-big"),
    ("out of memory", "out-of-memory"),
)


def ubsan_rule(message: str) -> str:
    for pattern, rule in _UBSAN_RULES_FIRST_MATCH_WINS:
        if pattern.search(message):
            return rule
    return UBSAN_FALLBACK_RULE


def _asan_kind(header_rest: str) -> str:
    description = header_rest.strip()
    without_attempting = _ASAN_ATTEMPTING_RE.sub("", description)
    if without_attempting.startswith("free on address which was not malloc"):
        return "bad-free"
    for phrase, kind in _ASAN_KIND_BY_PHRASE:
        if phrase in description:
            return kind
    word = _ASAN_KIND_WORD_RE.match(without_attempting)
    return word.group(0) if word else ASAN_FALLBACK_RULE


def runtime_internal_error_line(stderr: str) -> str | None:
    return next((line for line in stderr.splitlines() if _RUNTIME_INTERNAL_ERROR_RE.search(line)), None)


def strip_run_specific_details(text: str, base_dir: str | None) -> str:
    cleaned = _ADDRESS_GROUP_RE.sub("", text)
    cleaned = _ADDRESS_RE.sub("<addr>", cleaned)
    if base_dir:
        posix_base = base_dir.replace("\\", "/").rstrip("/") + "/"
        cleaned = cleaned.replace(posix_base, "")
        native_base = base_dir.rstrip("/\\") + os.sep
        cleaned = cleaned.replace(native_base, "")
    return cleaned.strip()


@dataclass
class LeakRecord:
    kind: str
    bytes: int
    objects: int
    frames: list[StackFrame] = field(default_factory=list)


@dataclass
class AsanContext:
    access: str | None = None
    located: str | None = None
    signal_cause: str | None = None
    hint: str | None = None
    variable: str | None = None

    def absorb(self, line: str) -> None:
        if (match := _ACCESS_RE.match(line)) and self.access is None:
            self.access = f"{match['acc']} of size {match['size']}"
        elif (match := _LOCATED_RE.search(line)) and self.located is None:
            self.located = match["what"]
        elif (match := _SIGNAL_CAUSE_RE.search(line)) and self.signal_cause is None:
            self.signal_cause = f"{match['acc']} memory access"
        elif (match := _HINT_RE.match(line)) and self.hint is None:
            self.hint = match["hint"]
        elif (match := _VARIABLE_RE.search(line)) and self.variable is None:
            self.variable = f"{match['what']} '{match['var']}'"

    def descriptions(self) -> list[str]:
        located = f"located {self.located}" if self.located else None
        return [text for text in (self.access, located, self.signal_cause, self.hint, self.variable) if text]


@dataclass
class ReportBlock:
    tool: SanitizerTool
    start: int
    header: str = ""
    header_kind: str | None = None
    ub_file: str | None = None
    ub_line: int | None = None
    ub_col: int | None = None
    ub_msg: str = ""
    summary_kind: str | None = None
    summary_rest: str | None = None
    end: int | None = None
    first_line_after_excerpt: int | None = None
    stacks: list[list[StackFrame]] = field(default_factory=list)
    leaks: list[LeakRecord] = field(default_factory=list)
    asan: AsanContext = field(default_factory=AsanContext)
    closed: bool = False

    def first_stack(self) -> list[StackFrame]:
        return self.stacks[0] if self.stacks else []

    def close_at(self, index: int) -> None:
        self.end, self.closed = index, True

    def absorb(self, index: int, line: str) -> None:
        if _EXCERPT_END_RE.match(line):
            if self.first_line_after_excerpt is None:
                self.first_line_after_excerpt = index
            return
        if leak := _LEAK_RECORD_RE.match(line):
            self.leaks.append(LeakRecord(leak["kind"], int(leak["bytes"]), int(leak["objs"])))
            return
        frame = parse_frame(line)
        if frame is not None:
            self._add_frame(frame)
            return
        if self.tool == "asan":
            self.asan.absorb(line)

    def _add_frame(self, frame: StackFrame) -> None:
        if self.tool == "lsan" and self.leaks:
            self.leaks[-1].frames.append(frame)
            return
        if frame.index == 0 or not self.stacks:
            self.stacks.append([])
        self.stacks[-1].append(frame)


def _report_header(index: int, line: str) -> ReportBlock | None:
    if match := _ASAN_HEADER_RE.match(line):
        return ReportBlock("asan", index, header=match["rest"], header_kind=_asan_kind(match["rest"]))
    if match := _LSAN_HEADER_RE.match(line):
        return ReportBlock("lsan", index, header=match["rest"], header_kind=MEMORY_LEAK_RULE)
    if match := _RSS_LIMIT_HEADER_RE.match(line):
        header = f"hard rss limit exhausted {match['rest']}".strip()
        return ReportBlock(TOOL_BY_RUNTIME_NAME[match["tool"]], index, header=header, header_kind=RSS_LIMIT_RULE)
    if match := _UBSAN_LOCATED_RE.match(line):
        return ReportBlock(
            "ubsan", index, ub_file=match["file"], ub_line=int(match["line"]), ub_col=int(match["col"]),
            ub_msg=match["msg"],
        )
    if match := _UBSAN_UNLOCATED_RE.match(line):
        place = match["file"]
        return ReportBlock("ubsan", index, ub_file=None if place.startswith(("<", "(")) else place, ub_msg=match["msg"])
    return None


def _summary_belongs_to(block: ReportBlock | None, summary_tool: SanitizerTool, is_leak_summary: bool) -> bool:
    if block is None or block.closed:
        return False
    return block.tool == summary_tool or (block.tool == "lsan" and is_leak_summary)


def _summary_without_report(index: int, line: str, summary: re.Match, summary_tool: SanitizerTool) -> ReportBlock:
    if LEAK_SUMMARY_MARKER in line:
        return ReportBlock("lsan", index, header=line.split(": ", 2)[-1], header_kind=MEMORY_LEAK_RULE)
    return ReportBlock(
        summary_tool, index, header=line, header_kind=summary["kind"], summary_kind=summary["kind"],
        summary_rest=summary["rest"],
    )


class _ReportScanner:
    def __init__(self) -> None:
        self.blocks: list[ReportBlock] = []
        self.current: ReportBlock | None = None

    def feed(self, index: int, line: str) -> None:
        header = _report_header(index, line)
        if header is not None:
            self._open(header)
            return
        if summary := _SUMMARY_RE.match(line):
            self._take_summary(index, line, summary)
            return
        if self.current is not None and not self.current.closed:
            self.current.absorb(index, line)

    def finish(self, line_count: int) -> list[ReportBlock]:
        if self.current is not None and self.current.end is None:
            self.current.end = line_count - 1
        return self.blocks

    def _open(self, block: ReportBlock) -> None:
        if self.current is not None and self.current.end is None:
            self.current.end = block.start - 1
        self.blocks.append(block)
        self.current = block

    def _take_summary(self, index: int, line: str, summary: re.Match) -> None:
        summary_tool = TOOL_BY_RUNTIME_NAME[summary["tool"]]
        is_leak_summary = LEAK_SUMMARY_MARKER in line
        current = self.current
        if current is not None and _summary_belongs_to(current, summary_tool, is_leak_summary):
            if current.tool != "lsan":
                current.summary_kind, current.summary_rest = summary["kind"], summary["rest"]
            current.close_at(index)
            return
        orphan = _summary_without_report(index, line, summary, summary_tool)
        self._open(orphan)
        orphan.close_at(index)


def scan_report_blocks(lines: list[str]) -> list[ReportBlock]:
    scanner = _ReportScanner()
    for index, raw_line in enumerate(lines):
        scanner.feed(index, raw_line.rstrip("\r"))
    return scanner.finish(len(lines))


def _excerpt(lines: list[str], block: ReportBlock) -> str:
    end = block.end if block.end is not None else len(lines) - 1
    if block.first_line_after_excerpt is not None:
        end = min(end, block.first_line_after_excerpt - 1)
    text = "\n".join(lines[block.start : end + 1][:RAW_EXCERPT_MAX_LINES])
    if len(text) > RAW_EXCERPT_MAX_CHARS:
        text = text[:RAW_EXCERPT_MAX_CHARS] + EXCERPT_TRUNCATION_MARKER
    return text


@dataclass
class SanitizerFinding:
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
        return [
            frame.short(display_path(frame.file, self.base_dir) if frame.file else None)
            for frame in self.frames[:CALL_STACK_MAX_FRAMES]
        ]


def _is_check_site_frame(
    frame: StackFrame, block: ReportBlock, path: str, project_files: set[str] | None, base: str | None
) -> bool:
    if not frame.file or frame.line not in (None, block.ub_line):
        return False
    return (
        frame.file == block.ub_file
        or frame.file.endswith("/" + block.ub_file.lstrip("./"))
        or project_relative_path(frame.file, project_files, base) == path
    )


def _ubsan_check_site(
    block: ReportBlock, frames: list[StackFrame], project_files: set[str] | None, base: str | None
) -> Location | None:
    if not block.ub_file:
        return None
    path = reportable_path(block.ub_file, project_files, base)
    if path is None:
        return None
    function = next(
        (frame.function for frame in frames if _is_check_site_frame(frame, block, path, project_files, base)), None
    )
    return Location(file=path, line=block.ub_line, column=block.ub_col, function=function)


def _ubsan_finding(block: ReportBlock, raw: str, project_files: set[str] | None, base: str | None) -> SanitizerFinding:
    frames = block.first_stack()
    rule = ubsan_rule(block.ub_msg) if block.ub_msg else UBSAN_FALLBACK_RULE
    location = _ubsan_check_site(block, frames, project_files, base)
    if location is None:
        frame, path = pick_frame(frames, project_files, base)
        location = location_from_frame(frame, path) or location_from_summary_tail(block.summary_rest, project_files, base)
    detail = strip_run_specific_details(block.ub_msg, base) if block.ub_msg else block.summary_kind or UNDEFINED_BEHAVIOR_KIND
    diagnostic = Diagnostic(
        tool="ubsan",
        rule_id=rule,
        severity="error",
        message=f"UndefinedBehaviorSanitizer: {detail}",
        location=location,
        path=frame_path_steps(frames, project_files, base),
        raw=raw,
    )
    return SanitizerFinding(diagnostic, frames, base)


def _asan_rule(block: ReportBlock) -> str:
    if block.summary_kind and block.header_kind != RSS_LIMIT_RULE:
        return block.summary_kind
    return block.header_kind or ASAN_FALLBACK_RULE


def _asan_finding(block: ReportBlock, raw: str, project_files: set[str] | None, base: str | None) -> SanitizerFinding:
    rule = _asan_rule(block)
    frames = block.first_stack()
    frame, path = pick_frame(frames, project_files, base)
    location = location_from_frame(frame, path) or location_from_summary_tail(block.summary_rest, project_files, base)
    title = TOOL_TITLES.get(block.tool, "AddressSanitizer")
    message = f"{title}: {block.header}" if rule == RSS_LIMIT_RULE else f"{title}: {rule}"
    descriptions = block.asan.descriptions()
    if descriptions:
        message += " (" + "; ".join(strip_run_specific_details(text, base) for text in descriptions) + ")"
    diagnostic = Diagnostic(
        tool=block.tool,
        rule_id=rule,
        severity="error",
        message=strip_run_specific_details(message, base),
        location=location,
        path=frame_path_steps(frames, project_files, base),
        raw=raw,
    )
    return SanitizerFinding(diagnostic, frames, base)


def _lsan_findings(
    block: ReportBlock, raw: str, project_files: set[str] | None, base: str | None
) -> list[SanitizerFinding]:
    if not block.leaks:
        message = f"LeakSanitizer: {strip_run_specific_details(block.header, base)}"
        diagnostic = Diagnostic(tool="lsan", rule_id=MEMORY_LEAK_RULE, severity="error", message=message, raw=raw)
        return [SanitizerFinding(diagnostic, [], base)]
    findings = []
    for leak in block.leaks:
        frame, path = pick_frame(leak.frames, project_files, base)
        location = location_from_frame(frame, path)
        message = f"LeakSanitizer: {leak.kind} leak of {leak.bytes} byte(s) in {leak.objects} object(s)"
        if location is not None:
            message += f" allocated from {location.short()}"
        diagnostic = Diagnostic(
            tool="lsan", rule_id=MEMORY_LEAK_RULE, severity="error", message=message, location=location, raw=raw
        )
        findings.append(SanitizerFinding(diagnostic, leak.frames, base))
    return findings


def _block_findings(
    block: ReportBlock, raw: str, project_files: set[str] | None, base: str | None
) -> list[SanitizerFinding]:
    if block.tool == "ubsan":
        return [_ubsan_finding(block, raw, project_files, base)]
    if block.tool == "lsan" and block.header_kind == MEMORY_LEAK_RULE:
        return _lsan_findings(block, raw, project_files, base)
    return [_asan_finding(block, raw, project_files, base)]


def _finding_key(finding: SanitizerFinding) -> tuple:
    d = finding.diagnostic
    return (d.tool, d.rule_id, d.message, d.location.short() if d.location else None)


def parse_sanitizer_findings(
    text: str,
    *,
    project_files: set[str] | None = None,
    base_dir: str | os.PathLike[str] | None = None,
) -> list[SanitizerFinding]:
    base = str(base_dir) if base_dir is not None else None
    normalised_files = {posixpath.normpath(p) for p in project_files} if project_files is not None else None
    lines = (text or "").splitlines()
    findings: list[SanitizerFinding] = []
    seen: set[tuple] = set()
    for block in scan_report_blocks(lines):
        for finding in _block_findings(block, _excerpt(lines, block), normalised_files, base):
            key = _finding_key(finding)
            if key not in seen:
                seen.add(key)
                findings.append(finding)
    return findings


def parse_sanitizer_output(
    text: str,
    *,
    project_files: set[str] | None = None,
    base_dir: str | os.PathLike[str] | None = None,
) -> list[Diagnostic]:
    return [f.diagnostic for f in parse_sanitizer_findings(text, project_files=project_files, base_dir=base_dir)]
