from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

ObjectFormat = Literal["elf", "macho", "coff", "bitcode", "unknown"]

ELF_MAGIC = b"\x7fELF"
BITCODE_MAGICS = frozenset({b"BC\xc0\xde", b"\xde\xc0\x17\x0b"})
MACHO_MAGICS = frozenset({
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
})
COFF_MACHINES = frozenset({b"\x64\x86", b"\x4c\x01", b"\x64\xaa", b"\xc0\x01"})
HEADER_READ_BYTES = 4

NM_CANDIDATES = ("llvm-nm-21", "llvm-nm-20", "llvm-nm-19", "llvm-nm-18", "llvm-nm", "nm")
READELF_CANDIDATES = ("llvm-readelf-21", "llvm-readelf-20", "llvm-readelf-19", "llvm-readelf-18", "llvm-readelf", "readelf")
OTOOL_CANDIDATES = ("otool",)
READOBJ_CANDIDATES = ("llvm-readobj-21", "llvm-readobj-20", "llvm-readobj-19", "llvm-readobj-18", "llvm-readobj")
OBJDUMP_CANDIDATES = ("llvm-objdump-21", "llvm-objdump-20", "llvm-objdump-19", "llvm-objdump-18", "llvm-objdump", "objdump")
DEMANGLER_CANDIDATES = ("llvm-cxxfilt-21", "llvm-cxxfilt-20", "llvm-cxxfilt-19", "llvm-cxxfilt-18", "llvm-cxxfilt", "c++filt")


@dataclass(frozen=True)
class SectionTool:
    name: str
    path: str
    fmt: str
    flags: tuple[str, ...]

    def argv(self, object_path: str) -> list[str]:
        return [self.path, *self.flags, object_path]


def object_format(path: str) -> ObjectFormat:
    try:
        with open(path, "rb") as fh:
            header = fh.read(HEADER_READ_BYTES)
    except OSError:
        return "unknown"
    if len(header) < 2:
        return "unknown"
    if header == ELF_MAGIC:
        return "elf"
    if header in BITCODE_MAGICS:
        return "bitcode"
    if header in MACHO_MAGICS:
        return "macho"
    if header[:2] in COFF_MACHINES:
        return "coff"
    return "unknown"


def select_section_tool(
    fmt: ObjectFormat,
    *,
    readelf: str | None,
    otool: str | None,
    readobj: str | None,
    objdump: str | None,
) -> SectionTool | None:
    if fmt == "macho":
        candidates = (
            SectionTool("otool", otool, "otool", ("-l",)) if otool else None,
            SectionTool("llvm-readobj", readobj, "llvm-readobj", ("--sections",)) if readobj else None,
            SectionTool("objdump", objdump, "objdump", ("-h",)) if objdump else None,
        )
    elif fmt == "coff":
        candidates = (
            SectionTool("llvm-readobj", readobj, "llvm-readobj", ("--sections",)) if readobj else None,
            SectionTool("objdump", objdump, "objdump", ("-h",)) if objdump else None,
        )
    else:
        candidates = (
            SectionTool("readelf", readelf, "readelf", ("-S", "-W")) if readelf else None,
            SectionTool("llvm-readobj", readobj, "llvm-readobj", ("--sections",)) if readobj else None,
            SectionTool("objdump", objdump, "objdump", ("-h",)) if objdump else None,
            SectionTool("otool", otool, "otool", ("-l",)) if fmt == "unknown" and otool else None,
        )
    return next((tool for tool in candidates if tool is not None), None)


def missing_section_tool_message(fmt: ObjectFormat) -> str:
    if fmt == "macho":
        return "otool / llvm-readobj / llvm-objdump not found: cannot check Mach-O object sections"
    if fmt == "coff":
        return "llvm-readobj / llvm-objdump / objdump not found: cannot check COFF object sections"
    return "readelf / llvm-readelf not found: cannot check object sections"


@dataclass(frozen=True)
class SectionTools:
    readelf: str | None
    otool: str | None
    readobj: str | None
    objdump: str | None

    def select(self, fmt: ObjectFormat) -> SectionTool | None:
        return select_section_tool(
            fmt, readelf=self.readelf, otool=self.otool, readobj=self.readobj, objdump=self.objdump
        )


def first_tool(names: tuple[str, ...], finder: Callable[[str], str | None]) -> str | None:
    for name in names:
        path = finder(name)
        if path:
            return path
    return None


def find_section_tools(finder: Callable[[str], str | None]) -> SectionTools:
    return SectionTools(
        readelf=first_tool(READELF_CANDIDATES, finder),
        otool=first_tool(OTOOL_CANDIDATES, finder),
        readobj=first_tool(READOBJ_CANDIDATES, finder),
        objdump=first_tool(OBJDUMP_CANDIDATES, finder),
    )
