"""Map the requested sanitizers onto clang flags, runtime options, exit codes and a sandbox-visible symbolizer."""

from __future__ import annotations

import glob
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass

EXIT_ASAN = 86
EXIT_UBSAN = 87
EXIT_LSAN = 88
SANITIZER_EXIT_CODES = {EXIT_ASAN: "asan", EXIT_UBSAN: "ubsan", EXIT_LSAN: "lsan"}
SHARED_EXIT_CODE_NOTE = (
    "the combined ASan+UBSan runtime shares one exitcode flag (UBSAN_OPTIONS is parsed last); "
    "findings are attributed from the parsed reports, not from exit codes"
)

SANITIZER_ORDER = ("address", "undefined", "leak")
LEAK_WITH_UBSAN_NEEDS_ASAN_NOTE = (
    "leak+undefined without address cannot be linked (standalone LSan runtime lacks UBSan handlers); "
    "using address,undefined (ASan includes LeakSanitizer)"
)

SANDBOX_VISIBLE_PREFIX = "/usr/"
SYMBOLIZER_ENV_VARS = ("ASAN_SYMBOLIZER_PATH", "UBSAN_SYMBOLIZER_PATH", "LSAN_SYMBOLIZER_PATH")
_CLANG_MAJOR_VERSION_RE = re.compile(r"clang version (\d+)")


@dataclass(frozen=True)
class SanitizerPlan:
    """How the requested sanitizers map onto clang flags and runtime options."""

    requested: tuple[str, ...]
    effective: tuple[str, ...]
    fsanitize: tuple[str, ...]
    detect_leaks: bool
    notes: tuple[str, ...] = ()

    @property
    def compile_flags(self) -> list[str]:
        if not self.fsanitize:
            return []
        return ["-fno-sanitize-recover=all", "-fsanitize=" + ",".join(self.fsanitize)]

    @property
    def uses_shadow_runtime(self) -> bool:
        """ASan and LSan reserve huge virtual address ranges, so RLIMIT_AS must not be set."""
        return "address" in self.fsanitize or "leak" in self.fsanitize


def _standalone_lsan_cannot_link_ubsan(address: bool, undefined: bool, leak: bool) -> bool:
    return leak and undefined and not address


def _needs_standalone_lsan(address: bool, leak: bool) -> bool:
    return leak and not address


def sanitizer_plan(requested: Iterable[str]) -> SanitizerPlan:
    """Map ``tests.sanitizers`` to ``-fsanitize=`` values (LeakSanitizer runs inside ASan when both are wanted)."""
    unique_requested = tuple(dict.fromkeys(requested))
    address = "address" in unique_requested
    undefined = "undefined" in unique_requested
    leak = "leak" in unique_requested
    notes: list[str] = []
    if _standalone_lsan_cannot_link_ubsan(address, undefined, leak):
        address = True
        notes.append(LEAK_WITH_UBSAN_NEEDS_ASAN_NOTE)
    wanted = {"address": address, "undefined": undefined, "leak": _needs_standalone_lsan(address, leak)}
    fsanitize = tuple(name for name in SANITIZER_ORDER if wanted[name])
    effective = tuple(name for name in SANITIZER_ORDER if name in fsanitize or (name == "leak" and leak))
    return SanitizerPlan(
        requested=unique_requested, effective=effective, fsanitize=fsanitize, detect_leaks=leak, notes=tuple(notes)
    )


def sanitizer_env(plan: SanitizerPlan, memory_mb: int, symbolizer: str | None) -> dict[str, str]:
    """Runtime options for the sanitized test binary."""
    rss_limit = f"hard_rss_limit_mb={int(memory_mb)}"
    asan_options = [
        f"detect_leaks={1 if plan.detect_leaks else 0}",
        "halt_on_error=1",
        "abort_on_error=0",
        f"exitcode={EXIT_ASAN}",
        rss_limit,
        "allocator_may_return_null=1",
        "detect_stack_use_after_return=1",
        "symbolize=1",
    ]
    env = {
        "ASAN_OPTIONS": ":".join(asan_options),
        "UBSAN_OPTIONS": f"print_stacktrace=1:halt_on_error=1:exitcode={EXIT_UBSAN}",
        "LSAN_OPTIONS": f"exitcode={EXIT_LSAN}:{rss_limit}",
    }
    if symbolizer:
        env.update(dict.fromkeys(SYMBOLIZER_ENV_VARS, symbolizer))
    return env


def exit_code_details() -> dict:
    return {"asan": EXIT_ASAN, "ubsan": EXIT_UBSAN, "lsan": EXIT_LSAN, "note": SHARED_EXIT_CODE_NOTE}


def _symbolizer_candidates(clang: str | None, clang_version: str | None) -> list[str]:
    candidates: list[str] = []
    if clang:
        candidates.append(os.path.join(os.path.dirname(os.path.realpath(clang)), "llvm-symbolizer"))
    major = _CLANG_MAJOR_VERSION_RE.search(clang_version or "")
    if major:
        candidates += [f"/usr/lib/llvm-{major.group(1)}/bin/llvm-symbolizer", f"/usr/bin/llvm-symbolizer-{major.group(1)}"]
    candidates.append("/usr/bin/llvm-symbolizer")
    candidates += sorted(glob.glob("/usr/lib/llvm-*/bin/llvm-symbolizer"), reverse=True)
    return candidates


def find_llvm_symbolizer(clang: str | None = None, clang_version: str | None = None) -> str | None:
    """An ``llvm-symbolizer`` under ``/usr`` (visible inside the sandbox), preferring the one next to ``clang``."""
    for candidate in _symbolizer_candidates(clang, clang_version):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            real = os.path.realpath(candidate)
            if real.startswith(SANDBOX_VISIBLE_PREFIX):
                return real
    return None
