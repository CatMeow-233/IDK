from __future__ import annotations

import shlex
import subprocess
from collections import Counter
from pathlib import Path

from leanc.toolchain.tools import find_clang

CC1_MARKER = '"-cc1"'
DRIVER_ONLY_FLAG = "-###"
COMPILE_ONLY_FLAG = "-c"
AUTOCOMPLETE_FLAG = "--autocomplete="
AUTOCOMPLETE_FIELD_SEPARATOR = "\t"
MINIMAL_PROGRAM = "int main(void) { return 0; }\n"


def clang() -> str:
    found = find_clang()
    assert found is not None
    return found


def minimal_source(tmp_path: Path, suffix: str) -> Path:
    source = tmp_path / f"probe{suffix}"
    source.write_text(MINIMAL_PROGRAM)
    return source


def cc1_args(source: Path, flags: list[str]) -> list[str]:
    proc = subprocess.run(
        [clang(), DRIVER_ONLY_FLAG, COMPILE_ONLY_FLAG, str(source), *flags], capture_output=True, text=True, check=False
    )
    [line] = [line for line in proc.stderr.splitlines() if CC1_MARKER in line]
    return shlex.split(line)


def cc1_changes(source: Path, flags: list[str]) -> tuple[Counter, Counter]:
    base, changed = Counter(cc1_args(source, [])), Counter(cc1_args(source, flags))
    return changed - base, base - changed


def option_spellings(prefix: str) -> list[str]:
    listed = subprocess.run([clang(), AUTOCOMPLETE_FLAG + prefix], capture_output=True, text=True, check=True).stdout
    return [line.split(AUTOCOMPLETE_FIELD_SEPARATOR)[0] for line in listed.splitlines() if line.strip()]
