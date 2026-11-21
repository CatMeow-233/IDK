from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from leanc.foundation.redact import redact
from leanc.foundation.runner import Limits, minimal_env, run_command
from leanc.foundation.sandbox import (
    NetworkIsolation,
    Sandbox,
    SandboxMounts,
    detect_sandbox,
    network_probe_blocked,
)
from leanc.specs.approval import leanc_home
from leanc.toolchain.cxx import find_clangxx
from leanc.toolchain.framac import INSTALL_HINT as WP_HINT
from leanc.toolchain.framac import find_framac
from leanc.toolchain.lean import INSTALL_HINT as LEAN_HINT
from leanc.toolchain.lean import find_lean
from leanc.toolchain.tools import find_cbmc, find_clang, find_tool, tool_version

APT_HINT = "sudo apt-get install -y clang-18 libclang-rt-18-dev cbmc gcc bubblewrap   (Ubuntu 24.04 / Debian)"
GCC = "gcc"
GCC_PURPOSE = "CBMC, Frama-C and the integrity gate's predefined-macro check preprocess with gcc"
MIN_PYTHON = (3, 11)
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

ANALYZER_PROBE_C = "int f(int *p){ if(!p) return *p; return 0; }\n"
SANITIZER_PROBE_C = (
    "#include <stdlib.h>\nint main(int c,char**v){int *p=malloc(4*sizeof(int)); int r=p[c]; free(p); return r*0;}\n"
)
CBMC_PROBE_C = (
    "#include <assert.h>\nint nondet_int(void);\nint main(void){int x=nondet_int(); if(x>0 && x<10) assert(x*2<20); "
    "return 0;}\n"
)
ANALYZER_TIMEOUT_S = 60
SANITIZER_BUILD_TIMEOUT_S = 120
SANITIZER_RUN_TIMEOUT_S = 30
CBMC_TIMEOUT_S = 120
SHOWN_STDERR_CHARS = 300
SHOWN_RUN_STDERR_CHARS = 200
CBMC_SUCCESS_MARKERS = ('"cProverStatus": "success"', '"cProverStatus":"success"')


@dataclass
class Check:
    name: str
    ok: bool
    required: bool
    detail: str
    fix: str = ""


def _tiny_c(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def _safe_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
    except ValueError:
        return "[unparsable URL]"
    return redact(urlunsplit((parts.scheme, host, parts.path, "", "")))


def _python_check() -> Check:
    ok = sys.version_info >= MIN_PYTHON
    return Check("python", ok, True, sys.version.split()[0], "" if ok else "Python 3.11+ is required")


def _missing_clang_checks() -> list[Check]:
    hint = (
        f"LEANC_CLANG={os.environ['LEANC_CLANG']!r} is not an executable"
        if os.environ.get("LEANC_CLANG")
        else f"install clang ({APT_HINT}) or set LEANC_CLANG"
    )
    return [
        Check("clang", False, True, "not found", hint),
        Check("clang-static-analyzer", False, True, "not checked: clang missing", hint),
        Check("sanitizers", False, True, "not checked: clang missing", hint),
    ]


def _sarif_finding_count(sarif: Path) -> int:
    return len(json.loads(sarif.read_text())["runs"][0]["results"])


def _static_analyzer_check(clang: str, workdir: Path) -> Check:
    src = _tiny_c(workdir, "sa.c", ANALYZER_PROBE_C)
    sarif = workdir / "sa.sarif"
    result = run_command(
        [clang, "--analyze", "--analyzer-output", "sarif", "-o", str(sarif), str(src)],
        cwd=workdir,
        limits=Limits(timeout_s=ANALYZER_TIMEOUT_S),
        env=minimal_env(),
    )
    ok = False
    detail = result.error or result.stderr.strip()[-SHOWN_STDERR_CHARS:]
    if sarif.exists():
        try:
            findings = _sarif_finding_count(sarif)
        except (ValueError, KeyError, IndexError) as exc:
            detail = f"SARIF unparsable: {exc}"
        else:
            ok = findings >= 1
            detail = f"SARIF output ok ({findings} finding(s) on probe file)"
    return Check("clang-static-analyzer", ok, True, detail, "" if ok else "clang --analyze with SARIF output must work")


def _sandboxed_sanitizer_run(exe: Path, workdir: Path, sandbox: Sandbox) -> tuple[bool, str]:
    run = run_command(
        [str(exe)],
        cwd=workdir,
        limits=Limits(timeout_s=SANITIZER_RUN_TIMEOUT_S),
        env=minimal_env({"ASAN_OPTIONS": "detect_leaks=1"}),
        sandbox=sandbox,
        mounts=SandboxMounts(ro=[str(workdir)]),
    )
    if run.exit_code == 0:
        return True, "; sandboxed run ok"
    return False, f"; sandboxed run exit {run.exit_code}: {run.stderr.strip()[-SHOWN_RUN_STDERR_CHARS:]}"


def _sanitizer_check(clang: str, workdir: Path, sandbox: Sandbox) -> Check:
    src = _tiny_c(workdir, "san.c", SANITIZER_PROBE_C)
    exe = workdir / "san"
    build = run_command(
        [clang, "-g", "-fsanitize=address,undefined", str(src), "-o", str(exe)],
        cwd=workdir,
        limits=Limits(timeout_s=SANITIZER_BUILD_TIMEOUT_S),
        env=minimal_env(),
    )
    if not (build.ok and exe.exists()):
        return Check(
            "sanitizers",
            False,
            True,
            (build.error or build.stderr.strip())[-SHOWN_STDERR_CHARS:],
            "install the sanitizer runtime, e.g. libclang-rt-18-dev",
        )
    if sandbox.active:
        ok, run_detail = _sandboxed_sanitizer_run(exe, workdir, sandbox)
    else:
        ok, run_detail = True, "; run skipped (no sandbox)"
    return Check("sanitizers", ok, True, "ASan+UBSan build ok" + run_detail, "" if ok else "install libclang-rt-18-dev")


def _gcc_check() -> Check:
    gcc = find_tool(GCC)
    if not gcc:
        return Check(GCC, False, True, "not found", f"install gcc ({APT_HINT}); {GCC_PURPOSE}")
    return Check(GCC, True, True, f"{gcc}: {tool_version(gcc)}")


def _cbmc_check(cbmc: str | None, workdir: Path) -> Check:
    if not cbmc:
        return Check("cbmc", False, True, "not found", f"install CBMC ({APT_HINT}) or set LEANC_CBMC")
    src = _tiny_c(workdir, "cb.c", CBMC_PROBE_C)
    result = run_command(
        [cbmc, str(src), "--signed-overflow-check", "--json-ui"],
        cwd=workdir,
        limits=Limits(timeout_s=CBMC_TIMEOUT_S),
        env=minimal_env(),
    )
    ok = any(marker in result.stdout for marker in CBMC_SUCCESS_MARKERS)
    return Check(
        "cbmc",
        ok,
        True,
        f"{cbmc}: {tool_version(cbmc)}"
        + ("" if ok else f"; probe failed: {(result.error or result.stderr)[-SHOWN_RUN_STDERR_CHARS:]}"),
        "" if ok else "CBMC needs a working C preprocessor (gcc or cc on PATH)",
    )


def _network_note(network: NetworkIsolation) -> str:
    return f"; network: {network.value}"


def _sandbox_check(sandbox_preference: str, sandbox: Sandbox, workdir: Path) -> Check:
    if sandbox_preference == "none":
        return Check("sandbox", True, False, "skipped (--no-sandbox); untrusted tests would be SKIPPED")
    if sandbox.active:
        network = network_probe_blocked(sandbox, workdir)
        blocked = network is NetworkIsolation.BLOCKED
        return Check(
            "sandbox",
            blocked,
            True,
            sandbox.note + _network_note(network),
            "" if blocked else "bwrap must isolate the network",
        )
    return Check(
        "sandbox",
        False,
        True,
        sandbox.note,
        "install bubblewrap and enable unprivileged user namespaces; without it Lean C refuses to execute "
        "untrusted test programs (static analysis and CBMC still run)",
    )


def _toolchain_checks(clang: str | None, cbmc: str | None, sandbox: Sandbox, sandbox_preference: str) -> list[Check]:
    checks: list[Check] = []
    with tempfile.TemporaryDirectory(prefix="leanc-doctor-") as temporary:
        workdir = Path(temporary)
        if clang:
            checks.append(Check("clang", True, True, f"{clang}: {tool_version(clang)}"))
            checks.append(_static_analyzer_check(clang, workdir))
            checks.append(_sanitizer_check(clang, workdir, sandbox))
        else:
            checks.extend(_missing_clang_checks())
        checks.append(_gcc_check())
        checks.append(_cbmc_check(cbmc, workdir))
        checks.append(_sandbox_check(sandbox_preference, sandbox, workdir))
    return checks


def _llm_provider_check() -> Check:
    key = bool(os.environ.get("LEANC_API_KEY") or os.environ.get("OPENAI_API_KEY"))
    model = os.environ.get("LEANC_MODEL")
    base = _safe_url(os.environ.get("LEANC_BASE_URL", DEFAULT_OPENAI_BASE_URL))
    custom_base = bool(os.environ.get("LEANC_BASE_URL"))
    configured = bool(model and (key or custom_base))
    if configured:
        detail = f"OpenAI-compatible provider configured (model {model}, base {base}{'' if key else ', no API key'})"
    elif key:
        detail = "API key present but LEANC_MODEL not set"
    else:
        detail = "no API key: offline mode (verification works; use --provider mock for generation/repair demos)"
    return Check(
        "llm-provider", configured, False, detail, "" if configured else "set LEANC_API_KEY and LEANC_MODEL (optional)"
    )


def _clangxx_check(clang: str | None) -> Check:
    cxx = find_clangxx(clang)
    return Check(
        "clang++ (C++20 specs)",
        bool(cxx),
        False,
        f"{cxx}: {tool_version(cxx)}" if cxx else "not found",
        "" if cxx else "install clang++ (same version as clang) or set LEANC_CLANGXX",
    )


def _framac_check() -> Check:
    framac = find_framac()
    if not framac:
        return Check("frama-c-wp (wp gate)", False, False, "not found", WP_HINT)
    detail = f"{framac.frama_c}: {tool_version(framac.frama_c)}" + (
        f"; why3 {framac.why3}" if framac.why3 else "; why3 missing"
    )
    return Check("frama-c-wp (wp gate)", bool(framac.why3), False, detail, "" if framac.why3 else WP_HINT)


def _lean_check() -> Check:
    lean = find_lean()
    if not lean:
        return Check("lean (lean gate)", False, False, "not found", LEAN_HINT)
    ok = bool(lean.leanchecker)
    return Check(
        "lean (lean gate)",
        ok,
        False,
        f"{lean.lean}: {lean.version_line or lean.version}" + ("; leanchecker ok" if ok else "; leanchecker MISSING"),
        "" if ok else "the lean gate requires leanchecker (ships with Lean 4 release toolchains)",
    )


def _approval_key_check() -> Check:
    key_path = leanc_home() / "approval.key"
    return Check(
        "approval-key",
        key_path.is_file(),
        False,
        f"{key_path} {'exists' if key_path.is_file() else 'will be created on first `leanc spec approve`'}",
    )


def run_doctor(sandbox_preference: str = "auto") -> list[Check]:
    checks = [_python_check()]
    clang = find_clang()
    cbmc = find_cbmc()
    sandbox = detect_sandbox(sandbox_preference)
    checks.extend(_toolchain_checks(clang, cbmc, sandbox, sandbox_preference))
    checks.append(_llm_provider_check())
    checks.append(_clangxx_check(clang))
    checks.append(_framac_check())
    checks.append(_lean_check())
    checks.append(_approval_key_check())
    return checks


def checks_to_json(checks: list[Check]) -> list[dict]:
    return [asdict(c) for c in checks]
