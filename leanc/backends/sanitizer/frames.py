from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from leanc.foundation.models import Location, PathStep

CALL_STACK_MAX_FRAMES = 20
SYMBOLISED_FRAME_PREFIX = "in "

SYSTEM_PATH_PREFIXES = (
    "/usr/",
    "/lib/",
    "/lib32/",
    "/lib64/",
    "/libx32/",
    "/opt/",
    "/build/",
    "/proc/",
    "/nix/",
    "/System/",
    "/Library/",
    "/Applications/Xcode.app/",
)
WINDOWS_SYSTEM_PATH_PREFIXES = ("c:/windows/", "c:/program files/", "c:/program files (x86)/")
LIBC_BUILD_DIRS = ("csu/", "sysdeps/", "nptl/", "stdlib/", "string/", "libio/", "malloc/", "elf/", "posix/", "misc/")
UNKNOWN_PATHS = frozenset({"??", "?"})
_WINDOWS_DRIVE_ABS_RE = re.compile(r"^[A-Za-z]:/")

MAX_REPORT_NUMBER_DIGITS = 18
REPORT_NUMBER = rf"\d{{1,{MAX_REPORT_NUMBER_DIGITS}}}(?!\d)"
_FRAME_RE = re.compile(rf"^\s*#(?P<n>{REPORT_NUMBER})\s+0x[0-9a-fA-F]+\s*(?P<rest>.*?)\s*$")
_BUILD_ID_SUFFIX_RE = re.compile(r"\s*\(BuildId: [0-9a-fA-F]+\)\s*$")
_FILE_POSITION_RE = re.compile(rf"^(?P<file>[^\s]+?):(?P<line>{REPORT_NUMBER})(?::(?P<col>{REPORT_NUMBER}))?$")
_MODULE_OFFSET_RE = re.compile(r"^(?P<func>.+?)\s+\((?P<module>[^()]*)\)$")
_SOURCE_FILE_WITHOUT_LINE_RE = re.compile(r"^(?:(?:/|[A-Za-z]:[/\\])\S+|\S+\.(?:c|h|cc|cpp|cxx|hpp|inc|S|s))$")
_SUMMARY_TAIL_LOCATION_RE = re.compile(
    rf"^(?P<file>\S+?):(?P<line>{REPORT_NUMBER})(?::(?P<col>{REPORT_NUMBER}))?(?:\s+in\s+(?P<func>\S+))?"
)


@dataclass
class StackFrame:
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
            position = where + (f":{self.line}" if self.line is not None else "")
            if self.column is not None and self.line is not None:
                position += f":{self.column}"
        else:
            position = f"({self.module})" if self.module else "?"
        return f"#{self.index} {self.function or '??'} {position}"


def _is_module_reference(module: str) -> bool:
    return "+0x" in module or module.startswith("<")


def _fill_symbolised_frame(frame: StackFrame, symbol: str) -> None:
    module = _MODULE_OFFSET_RE.match(symbol)
    if module and _is_module_reference(module["module"]):
        frame.function, frame.module = module["func"], module["module"]
        return
    function_and_place = symbol.rsplit(None, 1)
    if len(function_and_place) == 2:
        function, place = function_and_place
        if position := _FILE_POSITION_RE.match(place):
            frame.function = function
            frame.file = position["file"]
            frame.line = int(position["line"])
            frame.column = int(position["col"]) if position["col"] else None
            return
        if _SOURCE_FILE_WITHOUT_LINE_RE.match(place):
            frame.function, frame.file = function, place
            return
    frame.function = symbol or None


def parse_frame(line: str) -> StackFrame | None:
    match = _FRAME_RE.match(line)
    if not match:
        return None
    frame = StackFrame(index=int(match["n"]), text=line.strip())
    rest = _BUILD_ID_SUFFIX_RE.sub("", match["rest"]).strip()
    if rest.startswith(SYMBOLISED_FRAME_PREFIX):
        _fill_symbolised_frame(frame, rest[len(SYMBOLISED_FRAME_PREFIX) :].strip())
    elif rest.startswith("(") and rest.endswith(")"):
        frame.module = rest[1:-1]
    return frame


def relative_to_snapshot(path: str, base_dir: str | None) -> str | None:
    if not base_dir:
        return None
    normalised = path.replace("\\", "/")
    prefix = base_dir.replace("\\", "/").rstrip("/") + "/"
    if normalised.startswith(prefix):
        return posixpath.normpath(normalised[len(prefix) :])
    if _WINDOWS_DRIVE_ABS_RE.match(normalised) and normalised.lower().startswith(prefix.lower()):
        return posixpath.normpath(normalised[len(prefix) :])
    return None


def is_system_path(path: str) -> bool:
    normalised = path.replace("\\", "/")
    if not normalised or normalised.startswith("<") or normalised in UNKNOWN_PATHS:
        return True
    if normalised.startswith("/"):
        return normalised.startswith(SYSTEM_PATH_PREFIXES) or "/compiler-rt/" in normalised
    if _WINDOWS_DRIVE_ABS_RE.match(normalised):
        lower = normalised.lower()
        return lower.startswith(WINDOWS_SYSTEM_PATH_PREFIXES) or "/compiler-rt/" in lower
    return (
        normalised.startswith("../")
        or "/../" in normalised
        or normalised.startswith(LIBC_BUILD_DIRS)
        or "/sysdeps/" in normalised
    )


def _listed_below_snapshot(path: str, project_files: set[str] | None, base_dir: str | None) -> str | None:
    relative = relative_to_snapshot(path, base_dir)
    if relative is not None and (project_files is None or relative in project_files):
        return relative
    return None


def _listed_relative_path(path: str, project_files: set[str] | None) -> str | None:
    if path.startswith("/") or _WINDOWS_DRIVE_ABS_RE.match(path) or project_files is None:
        return None
    relative = posixpath.normpath(path)
    return relative if relative in project_files else None


def _longest_project_suffix(path: str, project_files: set[str] | None) -> str | None:
    if not project_files:
        return None
    matches = [candidate for candidate in project_files if path == candidate or path.endswith("/" + candidate)]
    return max(matches, key=len, default=None)


def project_relative_path(path: str, project_files: set[str] | None, base_dir: str | None) -> str | None:
    normalised = path.replace("\\", "/")
    return (
        _listed_below_snapshot(normalised, project_files, base_dir)
        or _listed_relative_path(normalised, project_files)
        or _longest_project_suffix(normalised, project_files)
    )


def display_path(path: str | None, base_dir: str | None) -> str | None:
    if not path:
        return None
    return relative_to_snapshot(path, base_dir) or path


def reportable_path(path: str, project_files: set[str] | None, base_dir: str | None) -> str | None:
    project_path = project_relative_path(path, project_files, base_dir)
    if project_path is not None:
        return project_path
    return None if is_system_path(path) else display_path(path, base_dir)


def pick_frame(
    frames: list[StackFrame], project_files: set[str] | None, base_dir: str | None
) -> tuple[StackFrame | None, str | None]:
    for frame in frames:
        if frame.file:
            project_path = project_relative_path(frame.file, project_files, base_dir)
            if project_path:
                return frame, project_path
    for frame in frames:
        if frame.file and not is_system_path(frame.file):
            return frame, display_path(frame.file, base_dir)
    return None, None


def location_from_frame(frame: StackFrame | None, path: str | None) -> Location | None:
    if frame is None:
        return None
    return Location(file=path, line=frame.line, column=frame.column, function=frame.function)


def location_from_summary_tail(
    text: str | None, project_files: set[str] | None, base_dir: str | None
) -> Location | None:
    if not text:
        return None
    match = _SUMMARY_TAIL_LOCATION_RE.match(text.strip())
    if not match:
        return None
    path = reportable_path(match["file"], project_files, base_dir)
    if path is None:
        return None
    column = int(match["col"]) if match["col"] else None
    return Location(file=path, line=int(match["line"]), column=column, function=match["func"])


def frame_path_steps(frames: list[StackFrame], project_files: set[str] | None, base_dir: str | None) -> list[PathStep]:
    steps = [
        PathStep(
            location=Location(
                file=project_relative_path(frame.file, project_files, base_dir) or display_path(frame.file, base_dir),
                line=frame.line,
                column=frame.column,
                function=frame.function,
            ),
            message=f"frame #{frame.index}",
        )
        for frame in frames
        if frame.file and not is_system_path(frame.file)
    ]
    return steps[:CALL_STACK_MAX_FRAMES]
