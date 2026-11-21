#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

from leanc import __version__
from leanc.foundation.status import EXIT_CODES
from leanc.toolchain.environment import toolchain

REPO = Path(__file__).resolve().parent.parent
DESCRIPTION = (
    "Run `leanc verify` and `leanc run --provider mock` on every examples/<name>/expected.yaml "
    "(throw-away LEANC_HOME) and compare the observed statuses with the expected ones."
)
LEANC_TIMEOUT_S = 900
CAPTURED_OUTPUT_CHARS = 2000
STATUS_EXIT_CODES = frozenset(EXIT_CODES.values())
SECRET_ENV_VARS = ("LEANC_API_KEY", "OPENAI_API_KEY")
EXPERIMENT_REVIEWER = "experiment-reviewer"
EXAMPLE_FAILURES = (OSError, RuntimeError, subprocess.SubprocessError, yaml.YAMLError)


def leanc(args: list[str], env: dict[str, str], cwd: Path, timeout: float = LEANC_TIMEOUT_S) -> tuple[int, dict, float]:
    t0 = time.monotonic()
    cp = subprocess.run(
        [sys.executable, "-m", "leanc", *args, "--json"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    dt = time.monotonic() - t0
    try:
        data = json.loads(cp.stdout) if cp.stdout.strip() else {}
    except json.JSONDecodeError:
        data = {"_unparsable_stdout": cp.stdout[-CAPTURED_OUTPUT_CHARS:]}
    if cp.returncode not in STATUS_EXIT_CODES:
        data["_stderr"] = cp.stderr[-CAPTURED_OUTPUT_CHARS:]
    return cp.returncode, data, dt


def detected_tools(payload: dict) -> list[str]:
    tools: set[str] = set()
    ver = payload.get("verification") or {}
    for f in ver.get("failures", []):
        if f.get("tool"):
            tools.add(f["tool"])
    return sorted(tools)


def approve(spec: Path, env: dict[str, str], cwd: Path) -> None:
    rc, data, _ = leanc(["spec", "approve", str(spec), "--by", EXPERIMENT_REVIEWER, "--yes"], env, cwd)
    if rc != 0:
        raise RuntimeError(f"approval failed for {spec}: {data}")


def _fresh_copy(ex: Path, dst: Path, env: dict[str, str]) -> Path:
    shutil.copytree(ex, dst, ignore=shutil.ignore_patterns(".leanc"))
    approve(dst / "spec.yaml", env, dst)
    return dst


def _verify_shipped_sources(vdir: Path, env: dict[str, str]) -> dict:
    rc, data, dt = leanc(["verify", str(vdir / "spec.yaml")], env, vdir)
    return {
        "status": data.get("status"),
        "exit_code": rc,
        "seconds": round(dt, 2),
        "detected_by": detected_tools(data),
        "stages": [
            {"stage": s.get("stage"), "outcome": s.get("outcome")}
            for s in (data.get("verification") or {}).get("stages", [])
        ],
    }


def _verify_variants(vdir: Path, exp: dict, env: dict[str, str]) -> dict:
    variants = {}
    for spec_name, vexp in (exp.get("variants") or {}).items():
        approve(vdir / spec_name, env, vdir)
        _, data, dt = leanc(["verify", str(vdir / spec_name)], env, vdir)
        variants[spec_name] = {"status": data.get("status"), "expected": vexp.get("verify"), "seconds": round(dt, 2)}
    return variants


def _run_with_mock_provider(rdir: Path, env: dict[str, str]) -> dict:
    rc, data, dt = leanc(["run", str(rdir / "spec.yaml"), "--provider", "mock", "--regenerate"], env, rdir)
    rounds = data.get("repair_rounds") or []
    return {
        "status": data.get("status"),
        "exit_code": rc,
        "seconds": round(dt, 2),
        "repair_rounds": len(rounds),
        "rejected_rounds": [r["round"] for r in rounds if r.get("policy_allowed") is False],
        "violations": sorted({v for r in rounds for v in r.get("violations", [])}),
    }


def _matches_expectations(row: dict, exp: dict) -> bool:
    ok = row["verify"]["status"] == exp.get("verify") and row["run"]["status"] == exp.get("run")
    for v in row["variants"].values():
        ok = ok and v["status"] == v["expected"]
    if "detected_by" in exp and exp["detected_by"]:
        ok = ok and set(exp["detected_by"]).issubset(set(row["verify"]["detected_by"]))
    if "rejected_rounds" in exp:
        ok = ok and row["run"]["rejected_rounds"] == list(exp["rejected_rounds"])
    return ok


def run_example(ex: Path, work: Path, env: dict[str, str]) -> dict:
    exp = yaml.safe_load((ex / "expected.yaml").read_text(encoding="utf-8")) or {}
    row: dict = {"example": ex.name, "expected": exp}
    vdir = _fresh_copy(ex, work / "verify" / ex.name, env)
    row["verify"] = _verify_shipped_sources(vdir, env)
    row["variants"] = _verify_variants(vdir, exp, env)
    rdir = _fresh_copy(ex, work / "run" / ex.name, env)
    row["run"] = _run_with_mock_provider(rdir, env)
    row["match"] = _matches_expectations(row, exp)
    return row


def _failed_example_row(ex: Path, exc: Exception) -> dict:
    return {
        "example": ex.name,
        "error": f"{type(exc).__name__}: {exc}",
        "match": False,
        "expected": {},
        "verify": {"status": None, "detected_by": []},
        "run": {"status": None, "repair_rounds": 0, "rejected_rounds": []},
        "variants": {},
    }


def render_md(rows: list[dict], meta: dict) -> str:
    lines = [
        "# Lean C experiment results",
        "",
        f"- date (UTC): {meta['date']}",
        f"- leanc: {meta['leanc_version']}; python {meta['python']}; {meta['platform']}",
        f"- tools: {meta['tools']}",
        "- provider for `run`: **mock** (fixed fixtures; NOT a measurement of LLM performance)",
        "",
        "| example | verify (expected) | detected by | run --provider mock (expected) | rounds | rejected rounds | match |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        e = r["expected"]
        lines.append(
            f"| {r['example']} | {r['verify']['status']} ({e.get('verify')}) | {', '.join(r['verify']['detected_by']) or '-'} "
            f"| {r['run']['status']} ({e.get('run')}) | {r['run']['repair_rounds']} | "
            f"{', '.join(map(str, r['run']['rejected_rounds'])) or '-'} | {'yes' if r['match'] else '**NO**'} |"
        )
        for spec_name, v in r["variants"].items():
            lines.append(f"| ↳ {r['example']}/{spec_name} | {v['status']} ({v['expected']}) | | | | | |")
    total = len(rows)
    good = sum(1 for r in rows if r["match"])
    lines += ["", f"**{good}/{total} examples match their expected statuses.**", ""]
    return "\n".join(lines)


def _experiment_env(work: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV_VARS}
    env["LEANC_HOME"] = str(work / "leanc_home")
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run_all(examples: list[Path]) -> list[dict]:
    rows = []
    with tempfile.TemporaryDirectory(prefix="leanc-exp-") as td:
        work = Path(td)
        env = _experiment_env(work)
        for ex in examples:
            print(f"== {ex.name}", flush=True)
            try:
                row = run_example(ex, work, env)
            except EXAMPLE_FAILURES as exc:
                row = _failed_example_row(ex, exc)
            print(f"   verify={row['verify']['status']} run={row['run']['status']} match={row['match']}", flush=True)
            rows.append(row)
    return rows


def _meta() -> dict:
    tc = toolchain()
    return {
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "leanc_version": __version__,
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "tools": f"{tc.clang_version}; {tc.cbmc_version and 'cbmc ' + tc.cbmc_version}",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--examples", default=str(REPO / "examples"))
    ap.add_argument("--out", default=str(REPO / "experiments" / "results"))
    ap.add_argument("--only", nargs="*", help="run only these example names")
    args = ap.parse_args()
    examples = sorted(p.parent for p in Path(args.examples).glob("*/expected.yaml"))
    if args.only:
        examples = [e for e in examples if e.name in set(args.only)]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = _run_all(examples)
    meta = _meta()
    (out / "results.json").write_text(json.dumps({"meta": meta, "results": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "results.md").write_text(render_md(rows, meta), encoding="utf-8")
    good = sum(1 for r in rows if r["match"])
    print(f"\n{good}/{len(rows)} examples match expected statuses; results in {out}")
    return 0 if good == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
