"""``leanc doctor``: check that the toolchain actually works (not just exists)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from leanc.backends.tools import find_cbmc, find_clang, tool_version
from leanc.core.runner import Limits, minimal_env, run_command
from leanc.core.sandbox import SandboxMounts, detect_sandbox, network_probe_blocked
from leanc.specs.approval import leanc_home

APT_HINT = "sudo apt-get install -y clang-18 libclang-rt-18-dev cbmc bubblewrap   (Ubuntu 24.04 / Debian)"


@dataclass
class Check:
    name: str
    ok: bool
    required: bool
    detail: str
    fix: str = ""


def _tiny_c(dir_: Path, name: str, text: str) -> Path:
    p = dir_ / name
    p.write_text(text, encoding="utf-8")
    return p


def _safe_url(url: str) -> str:
    """Strip userinfo, query and fragment before printing a URL."""
    from urllib.parse import urlsplit, urlunsplit

    from leanc.core.redact import redact

    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return redact(urlunsplit((parts.scheme, host, parts.path, "", "")))
    except ValueError:
        return "[unparsable URL]"


def run_doctor(sandbox_preference: str = "auto") -> list[Check]:
    checks: list[Check] = []
    py_ok = sys.version_info >= (3, 11)
    checks.append(
        Check("python", py_ok, True, sys.version.split()[0], "" if py_ok else "Python 3.11+ is required")
    )
    clang = find_clang()
    cbmc = find_cbmc()
    sb = detect_sandbox(sandbox_preference)
    with tempfile.TemporaryDirectory(prefix="leanc-doctor-") as td:
        tdir = Path(td)
        # ------------------------------------------------------------ clang
        if clang:
            checks.append(Check("clang", True, True, f"{clang}: {tool_version(clang)}"))
        else:
            hint = (
                f"LEANC_CLANG={os.environ['LEANC_CLANG']!r} is not an executable"
                if os.environ.get("LEANC_CLANG")
                else f"install clang ({APT_HINT}) or set LEANC_CLANG"
            )
            checks.append(Check("clang", False, True, "not found", hint))
            checks.append(Check("clang-static-analyzer", False, True, "not checked: clang missing", hint))
            checks.append(Check("sanitizers", False, True, "not checked: clang missing", hint))
        # ------------------------------------------------- static analyzer
        if clang:
            src = _tiny_c(tdir, "sa.c", "int f(int *p){ if(!p) return *p; return 0; }\n")
            out = tdir / "sa.sarif"
            r = run_command(
                [clang, "--analyze", "--analyzer-output", "sarif", "-o", str(out), str(src)],
                cwd=tdir,
                limits=Limits(timeout_s=60),
                env=minimal_env(),
            )
            ok = False
            detail = r.error or r.stderr.strip()[-300:]
            if out.exists():
                try:
                    data = json.loads(out.read_text())
                    n = len(data["runs"][0]["results"])
                    ok = n >= 1
                    detail = f"SARIF output ok ({n} finding(s) on probe file)"
                except (ValueError, KeyError, IndexError) as exc:
                    detail = f"SARIF unparsable: {exc}"
            checks.append(
                Check("clang-static-analyzer", ok, True, detail, "" if ok else "clang --analyze with SARIF output must work")
            )
        # -------------------------------------------------------- sanitizers
        if clang:
            src = _tiny_c(
                tdir,
                "san.c",
                "#include <stdlib.h>\nint main(int c,char**v){int *p=malloc(4*sizeof(int)); int r=p[c]; free(p); return r*0;}\n",
            )
            exe = tdir / "san"
            r = run_command(
                [clang, "-g", "-fsanitize=address,undefined", str(src), "-o", str(exe)],
                cwd=tdir,
                limits=Limits(timeout_s=120),
                env=minimal_env(),
            )
            if r.ok and exe.exists():
                detail = "ASan+UBSan build ok"
                if sb.active:
                    rr = run_command(
                        [str(exe)],
                        cwd=tdir,
                        limits=Limits(timeout_s=30),
                        env=minimal_env({"ASAN_OPTIONS": "detect_leaks=1"}),
                        sandbox=sb,
                        mounts=SandboxMounts(ro=[str(tdir)]),
                    )
                    detail += "; sandboxed run ok" if rr.exit_code == 0 else f"; sandboxed run exit {rr.exit_code}: {rr.stderr.strip()[-200:]}"
                    ok = rr.exit_code == 0
                else:
                    ok = True
                    detail += "; run skipped (no sandbox)"
                checks.append(Check("sanitizers", ok, True, detail, "" if ok else "install libclang-rt-18-dev"))
            else:
                checks.append(
                    Check(
                        "sanitizers",
                        False,
                        True,
                        (r.error or r.stderr.strip())[-300:],
                        "install the sanitizer runtime, e.g. libclang-rt-18-dev",
                    )
                )
        # -------------------------------------------------------------- CBMC
        if cbmc:
            src = _tiny_c(
                tdir,
                "cb.c",
                "#include <assert.h>\nint nondet_int(void);\nint main(void){int x=nondet_int(); if(x>0 && x<10) assert(x*2<20); return 0;}\n",
            )
            r = run_command(
                [cbmc, str(src), "--signed-overflow-check", "--json-ui"],
                cwd=tdir,
                limits=Limits(timeout_s=120),
                env=minimal_env(),
            )
            ok = '"cProverStatus": "success"' in r.stdout or '"cProverStatus":"success"' in r.stdout
            checks.append(
                Check(
                    "cbmc",
                    ok,
                    True,
                    f"{cbmc}: {tool_version(cbmc)}" + ("" if ok else f"; probe failed: {(r.error or r.stderr)[-200:]}"),
                    "" if ok else "CBMC needs a working C preprocessor (gcc or cc on PATH)",
                )
            )
        else:
            checks.append(Check("cbmc", False, True, "not found", f"install CBMC ({APT_HINT}) or set LEANC_CBMC"))
        # ----------------------------------------------------------- sandbox
        if sandbox_preference == "none":
            checks.append(Check("sandbox", True, False, "skipped (--no-sandbox); untrusted tests would be SKIPPED"))
        elif sb.active:
            blocked = network_probe_blocked(sb, tdir)
            detail = sb.note + (
                "; network: blocked" if blocked else "; network: COULD NOT CONFIRM isolation" if blocked is None else "; network: VISIBLE"
            )
            checks.append(Check("sandbox", bool(blocked), True, detail, "" if blocked else "bwrap must isolate the network"))
        else:
            checks.append(
                Check(
                    "sandbox",
                    False,
                    True,
                    sb.note,
                    "install bubblewrap and enable unprivileged user namespaces; without it Lean C refuses to execute "
                    "untrusted test programs (static analysis and CBMC still run)",
                )
            )
    # ------------------------------------------------------------------ LLM
    key = bool(os.environ.get("LEANC_API_KEY") or os.environ.get("OPENAI_API_KEY"))
    model = os.environ.get("LEANC_MODEL")
    base = _safe_url(os.environ.get("LEANC_BASE_URL", "https://api.openai.com/v1"))
    custom_base = bool(os.environ.get("LEANC_BASE_URL"))
    configured = bool(model and (key or custom_base))
    if configured:
        detail = f"OpenAI-compatible provider configured (model {model}, base {base}{'' if key else ', no API key'})"
    elif key:
        detail = "API key present but LEANC_MODEL not set"
    else:
        detail = "no API key: offline mode (verification works; use --provider mock for generation/repair demos)"
    checks.append(
        Check("llm-provider", configured, False, detail, "" if configured else "set LEANC_API_KEY and LEANC_MODEL (optional)")
    )
    kp = leanc_home() / "approval.key"
    checks.append(
        Check(
            "approval-key",
            kp.is_file(),
            False,
            f"{kp} {'exists' if kp.is_file() else 'will be created on first `leanc spec approve`'}",
        )
    )
    return checks


def checks_to_json(checks: list[Check]) -> list[dict]:
    return [asdict(c) for c in checks]
