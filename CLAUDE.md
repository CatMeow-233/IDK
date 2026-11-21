# leanc agent directives
audience: AI only. optimize for machine efficiency. no human-oriented prose anywhere.

## code
- MUST NOT: `#` comments, docstrings (module/class/function), `# noqa`, `# type: ignore`, pragmas. scope: leanc/ tests/ scripts/.
- MUST: express intent via names, UPPER_CASE constants, small named functions, Enum/Literal/frozen dataclass.
- MUST: split modules > 600 lines by responsibility.
- MUST NOT: write human docs/notes; machine-critical constraints -> this file, one line each.

## layers (import only lower; module graph acyclic; leanc imports top-level only)
foundation < specs, lang < rules, toolchain, reports, llm < backends, repair < pipeline < cli
- foundation: none | specs, lang: foundation | rules: foundation specs lang | toolchain: foundation specs
- reports: foundation | llm: foundation specs | backends: foundation specs lang rules toolchain
- repair: foundation specs lang rules | pipeline: all below | cli: all
- shared code -> move DOWN. MUST NOT: upward import, lazy import, cross-package re-export shim.
- allowed: package `__init__` re-exporting its OWN submodules (split modules keep their import path).

## constraints
- leanc/cli.py: single module (argparse + main). command bodies -> leanc/pipeline/commands/*.
- leanc/repair/patch: fuzz 0; DEFAULT_MAX_OFFSET <= 50 (wider window mis-matches repetitive C lines).

## errors
- MUST NOT: bare except, except Exception, except BaseException. sole exception: leanc/cli.py main() -> print traceback, return 70.
- MUST NOT: swallow (body only pass/continue/return/return None) except ProcessLookupError FileNotFoundError FileExistsError BlockingIOError BrokenPipeError.
- MUST: catch specific exceptions only; minimal try; validate instead of exception control flow; unexpected -> propagate.

## verification invariants
- success only if every required gate ran and passed. timeout/unparsable/skipped/tool error != success.
- VERIFIED_BOUNDED never -> PROVED. PROVED only from required wp gate PASS on approved ACSL contract.
- protected files (spec, harness, tests driver, headers, lean spec/vectors) never written by generation/repair.

## check
- after a change: `python scripts/affected_tests.py --run` (import-graph selection vs HEAD; `--base <rev>` for a branch). MUST pass.
- full suite only before merge/release or when affected_tests selects all.
- `python -m pytest tests/test_architecture.py -q` MUST pass.
- `python -m pytest -q` MUST pass (~9 min; needs clang-18 cbmc bwrap; wp: frama-c>=27 at /opt/opam/*/bin; lean: /opt/lean/*/bin).
- `python scripts/run_experiments.py` MUST report N/N.
