#!/usr/bin/env python3
"""Reproducible experiment runner for Lean C.

For every ``examples/<name>/expected.yaml`` this script

1. copies the example into a fresh temporary project,
2. approves its spec(s) with a throw-away signing key in a temporary
   ``LEANC_HOME`` (the example specs, harnesses and tests are human-authored;
   the script plays the human reviewer — your real key is never touched),
3. runs ``leanc verify`` on the shipped sources,
4. in a second fresh copy runs ``leanc run --provider mock`` (mock generation +
   verification + counterexample-driven repair with the example's mock fixtures),
5. verifies any spec variants listed in ``expected.yaml``,

and compares every observed status with the expected one.  Results are written
to ``experiments/results/results.json`` and ``experiments/results/results.md``.

The mock provider replays fixed fixtures: these numbers measure the
*verification pipeline* (detection, policy enforcement, status honesty), not the
performance of any LLM.

Usage::

    python scripts/run_experiments.py [--examples DIR] [--out DIR] [--only NAME ...]
"""

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

REPO = Path(__file__).resolve().parent.parent


def leanc(args: list[str], env: dict[str, str], cwd: Path, timeout: float = 900) -> tuple[int, dict, float]:
    t0 = time.monotonic()
    cp = subprocess.run(  # noqa: S603 - fixed argv
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
        data = {"_unparsable_stdout": cp.stdout[-2000:]}
    if cp.returncode not in (0, 10, 11, 12, 13, 20, 21, 30, 40, 50):
        data["_stderr"] = cp.stderr[-2000:]
    return cp.returncode, data, dt


def detected_tools(payload: dict) -> list[str]:
    tools: set[str] = set()
    ver = payload.get("verification") or {}
    for f in ver.get("failures", []):
        if f.get("tool"):
            tools.add(f["tool"])
    return sorted(tools)


def approve(spec: Path, env: dict[str, str], cwd: Path) -> None:
    rc, data, _ = leanc(["spec", "approve", str(spec), "--by", "experiment-reviewer", "--yes"], env, cwd)
    if rc != 0:
        raise RuntimeError(f"approval failed for {spec}: {data}")


def run_example(ex: Path, work: Path, env: dict[str, str]) -> dict:
    exp = yaml.safe_load((ex / "expected.yaml").read_text(encoding="utf-8")) or {}
    row: dict = {"example": ex.name, "expected": exp}

    # ---- verify the shipped sources
    vdir = work / "verify" / ex.name
    shutil.copytree(ex, vdir, ignore=shutil.ignore_patterns(".leanc"))
    approve(vdir / "spec.yaml", env, vdir)
    rc, data, dt = leanc(["verify", str(vdir / "spec.yaml")], env, vdir)
    row["verify"] = {
        "status": data.get("status"),
        "exit_code": rc,
        "seconds": round(dt, 2),
        "detected_by": detected_tools(data),
        "stages": [
            {"stage": s.get("stage"), "outcome": s.get("outcome")}
            for s in (data.get("verification") or {}).get("stages", [])
        ],
    }
    # ---- variants
    row["variants"] = {}
    for spec_name, vexp in (exp.get("variants") or {}).items():
        approve(vdir / spec_name, env, vdir)
        rc2, d2, dt2 = leanc(["verify", str(vdir / spec_name)], env, vdir)
        row["variants"][spec_name] = {"status": d2.get("status"), "expected": vexp.get("verify"), "seconds": round(dt2, 2)}

    # ---- full workflow with the mock provider
    rdir = work / "run" / ex.name
    shutil.copytree(ex, rdir, ignore=shutil.ignore_patterns(".leanc"))
    approve(rdir / "spec.yaml", env, rdir)
    rc, data, dt = leanc(["run", str(rdir / "spec.yaml"), "--provider", "mock", "--regenerate"], env, rdir)
    rounds = data.get("repair_rounds") or []
    row["run"] = {
        "status": data.get("status"),
        "exit_code": rc,
        "seconds": round(dt, 2),
        "repair_rounds": len(rounds),
        "rejected_rounds": [r["round"] for r in rounds if r.get("policy_allowed") is False],
        "violations": sorted({v for r in rounds for v in r.get("violations", [])}),
    }
    ok = row["verify"]["status"] == exp.get("verify") and row["run"]["status"] == exp.get("run")
    for v in row["variants"].values():
        ok = ok and v["status"] == v["expected"]
    if "detected_by" in exp and exp["detected_by"]:
        ok = ok and set(exp["detected_by"]).issubset(set(row["verify"]["detected_by"]))
    if "rejected_rounds" in exp:
        ok = ok and row["run"]["rejected_rounds"] == list(exp["rejected_rounds"])
    row["match"] = ok
    return row


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--examples", default=str(REPO / "examples"))
    ap.add_argument("--out", default=str(REPO / "experiments" / "results"))
    ap.add_argument("--only", nargs="*", help="run only these example names")
    args = ap.parse_args()
    examples = sorted(p.parent for p in Path(args.examples).glob("*/expected.yaml"))
    if args.only:
        examples = [e for e in examples if e.name in set(args.only)]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix="leanc-exp-") as td:
        work = Path(td)
        env = {k: v for k, v in os.environ.items() if k not in ("LEANC_API_KEY", "OPENAI_API_KEY")}
        env["LEANC_HOME"] = str(work / "leanc_home")
        env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
        for ex in examples:
            print(f"== {ex.name}", flush=True)
            try:
                row = run_example(ex, work, env)
            except Exception as exc:  # report, never hide
                row = {"example": ex.name, "error": f"{type(exc).__name__}: {exc}", "match": False,
                       "expected": {}, "verify": {"status": None, "detected_by": []},
                       "run": {"status": None, "repair_rounds": 0, "rejected_rounds": []}, "variants": {}}
            print(f"   verify={row['verify']['status']} run={row['run']['status']} match={row['match']}", flush=True)
            rows.append(row)
    from leanc import __version__
    from leanc.backends.tools import toolchain

    tc = toolchain()
    meta = {
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "leanc_version": __version__,
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "tools": f"{tc.clang_version}; {tc.cbmc_version and 'cbmc ' + tc.cbmc_version}",
    }
    (out / "results.json").write_text(json.dumps({"meta": meta, "results": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "results.md").write_text(render_md(rows, meta), encoding="utf-8")
    good = sum(1 for r in rows if r["match"])
    print(f"\n{good}/{len(rows)} examples match expected statuses; results in {out}")
    return 0 if good == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
