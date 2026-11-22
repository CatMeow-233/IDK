# Lean C

**English** · [简体中文](README.zh-Hans.md) · [繁體中文](README.zh-Hant.md)

Spec-first, verifier-checked AI code generation for C17 (plus C++20, Frama-C/WP and Lean 4 in V2). Research prototype.

An AI writes the code. Independent tools decide whether it is correct, and they check it against a spec that a human has approved and hash-locked. Those tools are Clang, the Clang Static Analyzer, ASan/UBSan/LSan, CBMC and, optionally, Frama-C/WP and Lean 4.

```
requirement -> spec draft (untrusted) -> human approval (hash-locked, HMAC-signed)
  -> code generation -> compile -> integrity gate -> static analysis -> sanitizer tests
  -> CBMC -> (V2) Frama-C/WP -> (V2, optional) Lean 4
  -> counterexample -> AI repair (policy-checked, max N rounds) -> re-verify -> JSON / Markdown report
```

Contents: [Install](#install) · [Quick start](#quick-start) · [Concepts](#concepts) · [Commands](#commands) · [Statuses and exit codes](#statuses-and-exit-codes) · [Gate rules](#gate-rules) · [Machine interface (AI agents / CI)](#machine-interface-ai-agents--ci) · [LLM providers](#llm-providers) · [Configuration](#configuration) · [V2 tools](#v2-optional-tools) · [Examples](#examples) · [Developing Lean C](#developing-lean-c) · [Limitations](#limitations)

---

## Install

You need Linux or WSL2 (the commands below are for Ubuntu 24.04) and Python 3.11 or newer.

```bash
sudo apt-get install -y clang-18 libclang-rt-18-dev cbmc gcc bubblewrap
pip install -e ".[dev]"          # runtime deps: pydantic, PyYAML; dev: pytest
leanc doctor                     # probes clang, analyzer, sanitizers, gcc, CBMC, bwrap sandbox + network isolation, LLM config
```

`leanc doctor` runs every tool for real and prints a fix for each failed check. Add `--json` for machine-readable output (`{"ok": bool, "checks": [{name, ok, required, detail, fix}]}`). If the bubblewrap sandbox is unavailable, sanitizer tests are skipped and the overall status can be at most `UNKNOWN` (see [Security](docs/security.md)).

## Quick start

All of the following runs offline; no API key is needed.

```bash
leanc init demo                                   # template: spec.yaml, include/, harness/, tests/, src/, leanc.yaml
leanc verify demo/spec.yaml                       # SPEC_UNAPPROVED (exit 40): nothing is claimed without an approved spec
# a human reviews demo/spec.yaml, demo/harness/, demo/tests/, demo/include/, then:
leanc spec approve demo/spec.yaml --by alice      # interactive confirmation; --yes skips it
leanc verify demo/spec.yaml                       # VERIFIED_BOUNDED (exit 0)
leanc report latest --project-root demo           # Markdown; --format json for JSON
```

The full loop, using a shipped example with a bug (generate → verify → counterexample → repair → re-verify):

```bash
cp -r examples/div_by_zero /tmp/dz
leanc spec approve /tmp/dz/spec.yaml --by alice --yes
leanc run /tmp/dz/spec.yaml --provider mock --regenerate
#   initial         COUNTEREXAMPLE_FOUND  (den == 0; INT_MIN / -1)
#   repair round 1: COUNTEREXAMPLE_FOUND  accepted (improvement; continuing)
#   repair round 2: VERIFIED_BOUNDED      accepted (verified)
#   STATUS          VERIFIED_BOUNDED
```

`--provider mock` replays the fixed fixtures in the example's `mock/` directory. It is deterministic and does not measure an LLM; reports label it as mock output.

## Concepts

| Term | Meaning |
|---|---|
| project root | nearest ancestor directory that contains `leanc.yaml`, otherwise the spec's directory; `--project-root` overrides it |
| spec (`spec.yaml`) | trusted contract: target signature, contract, sources, compile/analysis/test/CBMC settings, required `gates`. Schema: [docs/spec-format.md](docs/spec-format.md) |
| protected files | the spec, `sources.headers`, `verification.harness`, `tests.driver`, and V2 ACSL headers and Lean spec/vectors. Generation and repair never write them |
| editable files | `sources.editable`: the only files that generation and repair may write |
| approval | `leanc spec approve` binds the spec's semantic hash and the SHA-256 of every protected file into a record signed with a key in `LEANC_HOME` (default `~/.config/leanc`). Any change → `SPEC_UNAPPROVED` until re-approved, and `version` must increase. A rollback ledger rejects approvals that have been replaced |
| spec draft | `leanc spec draft` writes `drafts/<id>/` marked `provenance: ai-draft`. It is never trusted until a human reviews and approves it |
| gate | one verifier stage: `compile`, `integrity` (mandatory; runs after a passing compile gate), `static_analysis`, `sanitizer`, `cbmc`, and in V2 `wp` and `lean`. Success requires every required gate to run and pass |
| run | every `generate` / `verify` / `repair` / `run` creates `.leanc/runs/<run_id>/` |

Rule for any agent: never edit protected files, and never run `spec approve` on a human's behalf. Approval is the trust boundary.

## Commands

| Command | Purpose | Creates a run |
|---|---|---|
| `leanc doctor` | toolchain, sandbox and LLM configuration check | no |
| `leanc init [DIR] [--template safe-array-get] [--force]` | new project from a template | no |
| `leanc spec draft --id ID (--requirement TEXT \| --requirement-file F)` | requirement → untrusted draft in `drafts/<id>/` (exit 40) | no |
| `leanc spec approve SPEC --by NAME [--note N] [--yes]` | human approval (hash-lock + sign) | no |
| `leanc spec status SPEC` | approval state, signature validity, changes since approval | no |
| `leanc spec show SPEC` | normalized spec, hash, effective CBMC checks | no |
| `leanc generate SPEC [--force]` | write `sources.editable` for an approved spec (exit 0 = generated, **not** verified) | yes |
| `leanc verify SPEC` | run the verifier gates on the current sources | yes |
| `leanc repair SPEC [--max-rounds N] [--no-write]` | verify, then a counterexample-driven repair loop | yes |
| `leanc run SPEC [--regenerate] [--no-repair] [--max-rounds N] [--no-write]` | spec check → generate (if missing) → verify → repair → re-verify → report | yes |
| `leanc report [RUN\|latest] [--format md\|json]` | show a run's report; exits with that run's status, or 20 (STALE) if files changed after a successful run | no |

Common options:

| Option | Commands | Effect |
|---|---|---|
| `--json` | all except `report` | single JSON object on stdout; logs go to stderr |
| `-v` | all except `report` | verbose log to stderr |
| `--project-root DIR` | spec, generate, verify, repair, run, report | override project root |
| `--gates a,b` | verify, repair, run | run a subset of the spec's gates (result at most `UNKNOWN`) |
| `--fail-fast` | verify, repair, run | stop after the first gate that does not pass |
| `--timeout-scale X` | verify, repair, run | multiply every tool timeout |
| `--no-sandbox` | verify, repair, run | do not use bwrap; dynamic tests are `SKIPPED` → at most `UNKNOWN` |
| `--allow-unsandboxed` | verify, repair, run | DANGEROUS: run untrusted test programs without a sandbox (only this flag can do it; `leanc.yaml` cannot) |
| `--check-unapproved` | verify, repair, run | run the gates for information on an unapproved spec; the status stays `SPEC_UNAPPROVED` |
| `--provider mock\|openai`, `--mock-dir D`, `--llm-timeout S` | spec draft, generate, repair, run | LLM selection |

## Statuses and exit codes

| exit | status | meaning |
|---|---|---|
| 0 | `PROVED` | required `wp` gate proved the approved ACSL contract and all RTE guards (smoke tests clean). Only source of `PROVED` |
| 0 | `VERIFIED_BOUNDED` | no violation within the approved harness, input bounds and unwind bound; all loops fully unwound; harness non-vacuous. **Never promoted to `PROVED`** |
| 10 | `COMPILE_ERROR` | compiler errors (or warnings with `warnings_policy: block`) |
| 11 | `STATIC_ANALYSIS_WARNING` | analyzer finding with `policy: block`, or an integrity-gate violation |
| 12 | `RUNTIME_TEST_FAILED` | sanitizer report, a wrong exit code or output, or a Lean test-vector mismatch |
| 13 | `COUNTEREXAMPLE_FOUND` | CBMC found a concrete counterexample (a real execution path, valid even with an insufficient unwind) |
| 20 | `UNKNOWN` | inconclusive: unwind too small, vacuous harness, skipped gate, gate subset, no formal gate (C++), unproved goals |
| 21 | `TIMEOUT` | a required tool timed out |
| 30 | `TOOL_ERROR` | a tool crashed or produced unparsable output |
| 40 | `SPEC_UNAPPROVED` | no valid approval for the current spec and protected files |
| 50 | `REPAIR_EXHAUSTED` | repair loop ended without success; the workspace is restored |
| 2 | — | usage or input error |
| 70 | — | internal error (traceback on stderr) |

"No counterexample found" is not success. A timeout, a tool error, an unparsable result or a skipped gate never counts as success. The aggregation rules are in [docs/status-semantics.md](docs/status-semantics.md).

## Gate rules

These rules close ways in which a gate could pass code that was not really checked; the gaps that remain are listed under [Limitations](#limitations). None of them can turn a failure into a success.

| Rule | Behaviour |
|---|---|
| integrity not passed | `sanitizer`, `cbmc`, `wp` and `lean` run only after integrity passes. Otherwise they are `SKIPPED` ("never executed, model-checked or proved"), also when `gates` has no `compile`, because integrity runs after a passing compile gate. `static_analysis` still runs. The status comes from integrity: `FAIL` gives `STATIC_ANALYSIS_WARNING`, `ERROR` gives `TOOL_ERROR`, `TIMEOUT` gives `TIMEOUT`; when integrity did not run, the status is `UNKNOWN`. In the repair loop, a candidate that makes integrity pass counts as progress even when the unblocked gates then find counterexamples, provided no gate that ran before (`compile`, `static_analysis`) loses its pass or gains a failure |
| stack frames | integrity compiles each editable file with `-fstack-usage` (first `-include` of an anchor function, then `compile.flags` minus warning controls, then `-O0 -fno-lto -fembed-bitcode=off`) and reads the frame sizes clang writes: a frame larger than 1 MiB is `STACK_FRAME_TOO_LARGE`, and a frame clang marks `dynamic` (stack allocated at run time: a VLA, `alloca`, `_alloca`) is `UNBOUNDED_STACK`. The anchor makes clang write a stack-usage file for every file it generates code for; if there is none (for example under `-emit-llvm`) or the compile does not exit 0, integrity is `ERROR`. The VLA / `alloca` check (`UNBOUNDED_STACK`) also compiles with `compile.flags` and counts every diagnostic clang reports; a second run adds `-Wsystem-headers` and counts the diagnostics in the project's own files, so `-isystem` and similar options cannot hide a VLA in a protected header, while library headers such as libstdc++ are not counted |
| trigraphs | a trigraph (`??/`, `??=`, ...) anywhere in editable C code, comments and string literals included, is `TRIGRAPH`: clang replaces trigraphs in ISO C modes (`-std=c17`) while CBMC and Frama-C preprocess without them, so a `??/` that ends a comment hides the next line from one side only |
| predefined macros | only for specs with `cbmc` or `wp`. Clang's predefined macros under the compile gate's options (`-std`, `compile.flags`, `compile.defines`) are compared with gcc's under each verifier's own preprocessing. CBMC: `-D__CPROVER__ -m64 -std=gnuNN`, `-funsigned-char`, the `-D` defines it receives, then its x86-64 defines (`__LP64__`, `__x86_64__`, ...), in the order CBMC runs gcc. Frama-C: `-undef -nostdinc` with the machdep's builtin macros (`frama-c -print-machdep-builtin-macros`, which define `unix` and `linux`), `-D__FRAMAC__ -std=c11` and `compile.defines`. A use of a differing macro in editable code is `TOOL_DIVERGENCE`. Needs `gcc`, and `frama-c` for `wp`; if one is missing, integrity is `ERROR` |
| `compile.flags` | every entry is one option with its value joined to it (`-Iinclude`, `-DNAME=1`, `-std=c17`, `-includepre.h`). The spec validator rejects input files, `-` and `--`. The integrity gate asks clang (`clang -### <entry>`) whether an entry takes the next entry as its value (`-I include`, `-iprefix -funsigned-char`); such an entry is a `TOOL_ERROR`, because the CBMC translation and the stack checks read every entry as an option of its own. Also rejected: `-fsanitize*` / `-fno-sanitize*` (sanitizers come from `tests.sanitizers`), and options that hand arguments to an internal tool or replace the toolchain: `-X...` (`-Xclang`, `-Xanalyzer`, `-Xlinker`, ...) / `--for-linker`, `-mllvm`, `-Wp,` / `-Wa,` / `-Wl,`, `--analyze...`, `@file`, `--config`, `-fplugin` / `-fpass-plugin`, `-ccc-*`, `-B` / `--prefix`, `--ld-path` / `-fuse-ld=<path>`, `-resource-dir` / `--resource-dir`, `--sysroot` / `-isysroot`, `--gcc-toolchain` / `--gcc-install-dir`, `-nostdlib` / `--no-standard-libraries` / `-nodefaultlibs` / `-nostartfiles`, `--driver-mode`. Options that change what the preprocessor prints or stop before code generation are rejected as well, because integrity reads the preprocessed source with `compile.flags`: `-E`, `-P` / `--no-line-commands`, `-C` / `-CC`, `-d*` (`-dM`, `-dD`, ...), `-M` / `-MM`, `-fdirectives-only`, `-frewrite-includes`, `-fuse-line-directives`, `-fkeep-system-includes`, `-fminimize-whitespace`, `-traditional-cpp`, `-fsyntax-only`, `-fdriver-only`; preprocessed output without a linemarker for the file is `ERROR`. `-Wwrite-strings` is rejected: clang turns it into `-fconst-strings`, which gives string literals the type `const char[N]`, and no verifier applies that. Static analysis receives the remaining flags. CBMC receives `-D`, `-I`, `-std=` / `--std=` and `-funsigned-char` (signed `char` is CBMC's x86 default), also in their GCC long spellings (`--define-macro=`, `--undefine-macro=`, `--include-directory=`, `--unsigned-char`, `--signed-char`), but never `NDEBUG`. A `-U` only cancels an earlier `-D`, also of a function-like macro; any other `-U` is reported as not applied. Warning and `-g` flags have no semantic effect, except `-Wdeprecated` / `-Wno-deprecated`, which decide `__DEPRECATED` in C++ and are kept for the stack checks. Every other flag (for example `-O2 -ffast-math -mavx2`) is listed in the assumption line "compile.flags not applied by CBMC"; WP applies no `compile.flags` and lists them in "compile.flags not applied by Frama-C". The verdict is kept |
| body-less functions (CBMC) | allowed: `nondet_*`, `__CPROVER*`, `__builtin_prefetch`, `__builtin_expect`. Any other body-less callee keeps `cbmc` from passing: the SIMD builtins behind `<immintrin.h>`, and `memchr`, `strnlen`, `strstr`, `fputc` and `putc`, which integrity allows but CBMC has no model for. A violation whose trace uses the return value of such a callee is an `unmodelled-callee` diagnostic, not a counterexample; every other violation stays a counterexample. Without a counterexample the gate is `INCONCLUSIVE` (status `UNKNOWN`): replace the call with plain C to get a verdict |
| CBMC internal error | an invariant-violation report is `TOOL_ERROR` ("CBMC internal error") with the File/Condition/Reason excerpt, not an out-of-memory hint |
| contract safety checks | the checks in `contract.safety` are added to CBMC's property checks; the spec hash is unchanged |
| sanitizer builds | the test, ownership-trace and Lean vector builds add `-fno-builtin -fno-optimize-sibling-calls` after `compile.flags`, so clang `-O1` cannot delete unused allocations (`malloc`, `strdup`, `aligned_alloc`, C++ `new`) or the frames of wrapper functions |
| memory limitations | `limitations` lists what the leak checks could not see; a check counts only when its gate reached a verdict (`PASS` or `FAIL`). LeakSanitizer reports only unreachable blocks. Without a CBMC `memory-leak` verdict, leaks are checked only on the test paths. CBMC does not check alignment, strict aliasing, `restrict` or overflows between sub-objects of one allocation |

## Machine interface (AI agents / CI)

Use `--json` and branch on the exit code or on `status`. `leanc verify SPEC --json` prints:

```json
{
  "command": "verify", "status": "COUNTEREXAMPLE_FOUND", "exit_code": 13,
  "run_id": "verify-20261121T034210Z-7c8afd", "run_dir": ".../.leanc/runs/verify-...",
  "provider": null, "report": ".../report.md", "messages": ["..."],
  "verification": {
    "status": "...", "success": false, "spec_id": "...", "spec_version": 1, "spec_hash": "...",
    "approval": {"...": "..."},
    "stages": [{"stage": "cbmc", "outcome": "FAIL", "status": "...", "required": true, "invoked": true,
                "duration_s": 1.2, "summary": "...", "tools": [], "failures": []}],
    "detected_by": ["asan", "cbmc"], "reasons": ["..."], "assumptions": ["..."], "limitations": ["..."],
    "failures": [{
      "id": "sanitizer:index_equals_count:asan:heap-buffer-overflow", "tool": "asan", "stage": "sanitizer",
      "kind": "...", "property_id": "...", "location": {"file": "src/array_lookup.c", "line": 9, "column": 16, "function": "array_lookup"},
      "inputs": [{"name": "argv", "value": "index_equals_count", "type": null}],
      "description": "...", "raw_ref": "raw/...", "ownership_trace": null
    }]
  }
}
```

Run directory layout:

```
.leanc/runs/<run_id>/
  run.json  report.md  leanc.log
  verify/                         (repair rounds: repair/round-N/verify/)
    verification.json  verification.md
    snapshot/   exact sources that were verified
    raw/        raw tool output: cbmc-verify.json, compile/, integrity/, static_analysis/ (SARIF), sanitizer/, wp/, lean/
    logs/       stdout / stderr of every tool call, numbered in execution order
    build/
```

For a LeakSanitizer failure in a C project, `ownership_trace` holds evidence from an instrumented replay of the leaking test case. It is never part of the verdict. C++ and multi-threaded test programs are not traced. It contains:

- `allocation`: the allocation site.
- `released_at`: where allocations from that site are freed on other paths.
- `loss` and `function`: an upper bound on when the last reference disappeared. Only a probe that lists the block as a Direct leak counts as evidence; a scan that still finds a pointer-like word proves nothing, because LeakSanitizer scans conservatively. `loss` is one of:
  - `lost-during-call`: the block was allocated during the call to `function` and was already unreachable before `function` returned, so it was lost in `function` or a callee. This is the only strong claim.
  - `unreachable-by-return`: no reference remained by the time `function` (not on the allocation stack) was about to return. This says nothing about where the block was lost.
  - `unreachable-by-next-event`: the block was lost in `function` or its caller before the first traced event after `function` returned.
  - `not-localised`: no probe found the block unreachable before the program exited. `released_at` is still reported.
- `returned_to`: the call site `function` returned to. It is omitted when the callee was inlined.

The repair prompt shows the same evidence with an explicit label. It does not identify which `return` statement ran.

There are two ways for an agent to fix code:

1. **External agent loop.** Run `leanc verify SPEC --json`. Read `verification.failures` (location, inputs, property) and `raw_ref`. Edit only the files listed in `sources.editable`, then verify again. Stop on exit 0, or on 40 (needs a human), 20/21/30 (not a code bug: inspect `reasons`).
2. **Built-in loop.** Run `leanc run SPEC --provider openai --json` (or `repair`). Lean C calls the model, policy-checks every patch and writes back only a verified candidate (`--no-write` keeps the workspace unchanged).

Repair policy rejects these patches: writes outside `sources.editable`, `__CPROVER_*` / `LEANC_*` / `__builtin_assume`, deleting `assert`, suppressing warnings or sanitizers, `#include` escapes, `system`/`exec`/`fork`/`socket`, inline asm, hollowing out the target function, and oversized patches. Repeated patches are detected as cycles. A change toward `UNKNOWN`/`TIMEOUT` is not an improvement.

## LLM providers

**mock** (the default when `leanc.yaml` sets no provider). It reads fixtures from `<project>/mock/` (override with `--mock-dir`):

```
mock/
  spec_draft.yaml, harness_draft.c, tests_draft.c    returned by `spec draft`
  generate/<editable paths>        e.g. generate/src/safe_ratio.c
  repair/round-<N>.json            {"root_cause": "...", "rationale": "...", "files": {"src/x.c": "<full content>"}}
                                   or "patch": "<unified diff>" instead of "files"; a missing round = no proposal
```

**openai** (any OpenAI-compatible chat-completions endpoint):

| Variable | Meaning |
|---|---|
| `LEANC_API_KEY` (or `OPENAI_API_KEY`) | API key. Redacted from logs, reports and transcripts; never passed to subprocesses. It may be empty for a local server set through `LEANC_BASE_URL` |
| `LEANC_MODEL` | model name (required) |
| `LEANC_BASE_URL` | default `https://api.openai.com/v1` |
| `LEANC_LLM_TIMEOUT` | request timeout in seconds |
| `LEANC_LLM_MAX_RETRIES` | retries for transient errors |
| `LEANC_LLM_MAX_TOKENS` | max completion tokens (raise it if you see `finish_reason=length`) |
| `LEANC_LLM_TEMPERATURE` | number or `default` |
| `LEANC_LLM_JSON_MODE=1` | send `response_format: json_object` |
| `LEANC_LLM_PROXY` | `env` (use `HTTPS_PROXY`) or `none` |

```bash
export LEANC_API_KEY=... LEANC_MODEL=<model>
leanc spec draft --id my-func --requirement "Implement ..." --provider openai   # untrusted draft -> review -> approve
leanc run path/to/spec.yaml --provider openai --json
```

Prompts are in `leanc/llm/prompts/`. Record the model, temperature and prompt version yourself when you evaluate a model.

## Configuration

Optional `leanc.yaml` at the project root:

```yaml
provider: mock        # default provider: mock | openai
mock_dir: mock
sandbox: auto         # auto | none
```

The project config cannot weaken checks, bounds, gates, approval or isolation. If it sets `allow_unsandboxed`, that value is ignored with a warning.

| Variable | Meaning |
|---|---|
| `LEANC_HOME` | approval key and ledger directory (default `~/.config/leanc`); keep it outside the workspace |
| `LEANC_CLANG`, `LEANC_CLANGXX`, `LEANC_CBMC` | tool paths |
| `LEANC_FRAMAC` | `frama-c` (>= 27, with WP); otherwise `PATH` and `/opt/opam/*/bin` are searched |
| `LEANC_LEAN` | `lean` (toolchain with `leanchecker`); otherwise `PATH` and `/opt/lean/*/bin` are searched |

## V2 optional tools

| Feature | Install | Spec | Status effect |
|---|---|---|---|
| Frama-C/WP + ACSL | `sudo apt-get install -y opam z3 cvc5 && opam init -y && opam install -y frama-c alt-ergo` (Ubuntu's `frama-c-base` has no WP) | ACSL contract in a protected header, gate `wp` | only route to `PROVED`; proof repair lets the AI add loop invariants |
| Lean 4 | release toolchain unpacked to `/opt/lean/<version>` or via elan | protected model and theorem statements; the AI writes proofs; kernel + `leanchecker`; standard axioms only; differential test vectors | model level: unproved → `UNKNOWN`, vector mismatch → `RUNTIME_TEST_FAILED`; never `PROVED` |
| C++20 | `clang++` (part of clang-18) | `language: {name: cpp, standard: c++20}` | no C++ model checker is available, so the best result is `UNKNOWN` |

`leanc doctor` reports each of these tools. When a tool is missing, its tests are skipped rather than failed.

## Examples

Every `examples/<name>/` contains `spec.yaml`, protected `include/`, `harness/` and `tests/`, shipped (usually buggy) `src/`, `mock/` fixtures, and `expected.yaml`.

| Example | Shipped code | `verify` | `run --provider mock` |
|---|---|---|---|
| `safe_array_get` | correct | `VERIFIED_BOUNDED` | `VERIFIED_BOUNDED` |
| `array_oob` | `index <= count` | `COUNTEREXAMPLE_FOUND` (cbmc, asan) | 1 round → `VERIFIED_BOUNDED` |
| `div_by_zero` | unchecked `den` | `COUNTEREXAMPLE_FOUND` (cbmc, ubsan) | 2 rounds → `VERIFIED_BOUNDED` |
| `int_overflow` | add, then check | `COUNTEREXAMPLE_FOUND` | 1 round → `VERIFIED_BOUNDED` |
| `null_deref` | NULL check without return | `COUNTEREXAMPLE_FOUND` (analyzer, cbmc, ubsan) | 1 round → `VERIFIED_BOUNDED` |
| `loop_unwind` | correct, unwind too small | `UNKNOWN` | `UNKNOWN` (not "repaired"); variant `spec_full_unwind.yaml` → `VERIFIED_BOUNDED` |
| `vacuous_harness` | contradictory assumes | `UNKNOWN` (plain CBMC says SUCCESSFUL) | `UNKNOWN` |
| `runtime_memory` | leak on error path | `RUNTIME_TEST_FAILED` (lsan) | 1 round → `VERIFIED_BOUNDED` |
| `ring_buffer` | two `.c` files, correct | `VERIFIED_BOUNDED` | `VERIFIED_BOUNDED` |
| `cheating_repair` | off-by-one | `COUNTEREXAMPLE_FOUND` | rounds 1–4 rejected by policy, round 5 → `VERIFIED_BOUNDED` |
| `repair_exhausted` | `(a+b)/2` overflow | `COUNTEREXAMPLE_FOUND` | `REPAIR_EXHAUSTED`, workspace unchanged |
| `cbmc_timeout` | hard 64-bit equivalence | `TIMEOUT` | `TIMEOUT` |
| `wp_safe_array_get` | ACSL contract, correct | `PROVED` | `PROVED` |
| `wp_proof_repair` | correct, no loop invariant | `UNKNOWN` | 2 proof-repair rounds → `PROVED` |
| `wp_contract_violation` | violates postcondition | `COUNTEREXAMPLE_FOUND` | 1 round → `PROVED` |
| `lean_clamp` | Lean theorems left as `sorry` | `UNKNOWN` | 2 rounds → `VERIFIED_BOUNDED` |
| `cpp_ring_buffer` | C++20 class template | `UNKNOWN` | `UNKNOWN` |
| `cpp_bounds_bug` | C++ off-by-one | `RUNTIME_TEST_FAILED` (asan) | 1 round → `UNKNOWN` |

Reproduce all of them:

```bash
python scripts/run_experiments.py                       # every example; expect N/N matches
python scripts/run_experiments.py --only array_oob div_by_zero
```

The script copies each example to a temporary project and approves it with a temporary `LEANC_HOME`. It then runs `verify` and `run --provider mock --regenerate` through the real CLI and compares the outcome with `expected.yaml`. Output goes to `experiments/results/` (local, gitignored). Details: [docs/experiments.md](docs/experiments.md).

## Developing Lean C

The rules are declared in [`constraints.toml`](constraints.toml). It plays the role a yarn constraints file plays in a yarn project, and it is the single source of truth. `python -m scripts.constraints` checks those rules, and `tests/test_architecture.py` runs the same engine. Agent directives are in [`CLAUDE.md`](CLAUDE.md); its layer block is generated from `constraints.toml`.

- Strict one-way layering, an acyclic module graph, and top-level imports only:
  ```
  foundation < specs, lang, reports < rules, toolchain, llm < backends, repair < pipeline < cli < __main__
  ```
  Shared code moves down. A package `__init__` may re-export only its own submodules.
- No comments or docstrings in `leanc/`, `tests/` or `scripts/`. Express intent through names, constants, Enums and frozen dataclasses.
- No bare/`Exception`/`BaseException` handlers (the only exception is `leanc/cli.py` `main()`, which exits 70) and no swallowed exceptions.
- Every `.py` file has at most 600 lines.

| Package | Role |
|---|---|
| `foundation` | statuses, models, errors, hashing, redaction, workspace (path/symlink safety), sandbox, `run_command` (argv only, limits) |
| `specs` | spec schema, loading, hashing, approval and ledger |
| `lang` | C / C++ / Lean lexers |
| `rules` | pure trust rules shared by gates and repair policy (C, C++, ACSL, Lean, preprocessor) |
| `toolchain` | tool discovery, `BackendContext` |
| `reports` | JSON / Markdown rendering |
| `llm` | provider interface, mock, OpenAI-compatible, prompts, response parsing |
| `backends` | gates: clang, analyzer, integrity, sanitizer, cbmc, counterexample, wp, lean |
| `repair` | patch application (fuzz 0), generation and repair policy |
| `pipeline` | verification controller, generation, repair loop, doctor, config, command bodies |
| `cli.py` | argparse + `main` only |

Checks:

```bash
python -m scripts.constraints                       # all rules from constraints.toml; exit 1 on violations; --json
python -m scripts.constraints --fix                 # remove comments/docstrings only if the AST stays identical; regenerate the CLAUDE.md layer block
python scripts/affected_tests.py --run              # after a change: tests selected through the import graph vs HEAD
python scripts/affected_tests.py --base main --run  # for a whole branch
python -m scripts.merge_gate                        # before creating or merging a PR (~20 min)
```

`python -m scripts.merge_gate` enforces fix → test → merge on the exact HEAD that will be merged. It runs these steps in order and stops at the first failure; there is no skip option:

1. Fetch the base with an explicit refspec. The checkout must be committed, HEAD must differ from the base, and HEAD must contain the base.
2. Check out the pinned HEAD into a fresh temporary worktree. Every later step runs there, with all `PYTEST_*`/`PYTHON*` variables stripped, and the gate verifies that `leanc` is imported from that worktree.
3. Run `python -m scripts.constraints`.
4. Run the full suite. The junit report must show more than 0 tests with no failures, errors or skips; a skip means a required tool is missing.
5. Run `run_experiments.py`, which must report N/N with N > 0.
6. Check approval compatibility: for every example directory unchanged since the base, an approval made with the base code must still be valid under the HEAD code.
7. Confirm the worktree is still at the pinned HEAD and clean.

On success it prints `MERGE GATE PASS head=<sha> base=<sha>`. Right before merging, fetch and confirm that the remote base still equals that base sha (otherwise run the gate again), then merge only that head sha (`expectedHeadSha`, merge commit). Any new commit requires running the gate again.

The full suite needs clang-18, gcc, CBMC, bwrap and an x86-64 CPU with AVX2 (SIMD tests execute AVX2 code). WP tests, and the integrity tests of `wp` specs, need Frama-C >= 27 with `-print-machdep-builtin-macros` (tested with 33); Lean tests need Lean 4. When a tool is absent its tests are skipped, so the merge gate needs all of them.

## Limitations

- `VERIFIED_BOUNDED` is bounded to the approved harness. A human still has to judge whether the harness captures the requirement.
- Sanitizers only cover the paths that the trusted tests execute.
- CBMC does not model body-less functions (such as SIMD intrinsics) or flags it cannot apply (such as `-ffast-math`, `-O2`, `-m*`): the first make the result inconclusive, the second are disclosed as assumptions. WP applies no `compile.flags`: they are disclosed, and a proof under Frama-C's signed-`char` machdep can still be `PROVED` for code built with `-funsigned-char`.
- The macro comparison sees only predefined macros used by name. A predefined macro name built by token pasting (`##`), and a macro defined by a system header that differs between the tools (for example `M_PI`, which glibc defines under `-std=gnu11` but not under `-std=c17`), are not compared: code that branches on them can make the compile gate and a verifier see different code.
- The `compile.flags` check is a list of rejected options, not a list of allowed ones.
- The frame-size check measures each function at `-O0`. Inlining at a higher `-O` can merge frames, and the depth of a call chain is not checked.
- `PROVED` holds only relative to the approved ACSL contract, under WP's memory and machine model.
- The repair policy is defense in depth (textual rules). The real defense is the protected harness plus independent verifiers.
- C++20 has no formal gate (CBMC cannot parse modern C++, and ESBMC is not integrated), so the best C++ result is `UNKNOWN`.
- Lean proves a model, linked to the C code only through differential test vectors.
- bubblewrap is not a VM. Run high-risk workloads inside a separate container or VM.

Further documentation (Traditional Chinese): [architecture](docs/architecture.md) · [spec format](docs/spec-format.md) · [status semantics](docs/status-semantics.md) · [trust model](docs/trust-model.md) · [security](docs/security.md) · [experiments](docs/experiments.md) · [roadmap](docs/roadmap.md)
