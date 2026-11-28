# Lean C

**English** · [简体中文](README.zh-Hans.md) · [繁體中文](README.zh-Hant.md)

Spec-first, verifier-checked AI code generation for C17 (plus C++20, Frama-C/WP and Lean 4 in V2). Research prototype.

An AI writes the code. Independent tools decide whether it is correct, and they check it against a spec that a human has approved and hash-locked. Those tools are Clang, the Clang Static Analyzer, ASan/UBSan/LSan, CBMC and, optionally, CBMC loop contracts (the opt-in inductive profile), Frama-C/WP and Lean 4.

```
requirement -> spec draft (untrusted) -> human approval (hash-locked, HMAC-signed)
  -> code generation -> compile -> integrity gate -> static analysis -> sanitizer tests
  -> CBMC -> (opt-in) inductive CBMC loop contracts -> (V2) Frama-C/WP -> (V2, optional) Lean 4
  -> counterexample -> AI repair (policy-checked, max N rounds) -> re-verify -> JSON / Markdown report
```

Contents: [Install](#install) · [Quick start](#quick-start) · [Concepts](#concepts) · [Commands](#commands) · [Statuses and exit codes](#statuses-and-exit-codes) · [Gate rules](#gate-rules) · [Machine interface (AI agents / CI)](#machine-interface-ai-agents--ci) · [LLM providers](#llm-providers) · [Configuration](#configuration) · [V2 tools](#v2-optional-tools) · [Examples](#examples) · [Developing Lean C](#developing-lean-c) · [Limitations](#limitations)

---

## Install

You need Linux or WSL2 (the commands below are for Ubuntu 24.04) and Python 3.11 or newer.

```bash
sudo apt-get install -y clang-18 libclang-rt-18-dev llvm-18 cbmc gcc bubblewrap
pip install -e ".[dev]"          # runtime deps: pydantic, PyYAML; dev: pytest
leanc doctor                     # probes clang, analyzer, sanitizers, gcc, CBMC, goto-cc, goto-instrument, bwrap sandbox + network isolation, LLM config
```

`leanc doctor` runs every tool for real and prints a fix for each failed check; it exits 30 when a required check fails. Add `--json` for machine-readable output (`{"ok": bool, "checks": [{name, ok, required, detail, fix}]}`). If the bubblewrap sandbox is unavailable, sanitizer tests are skipped and the overall status can be at most `UNKNOWN` (see [Security](docs/security.md)).

The `cbmc` package of Ubuntu 24.04 is CBMC 5.95.1 and also installs `goto-cc` and `goto-instrument`, which only the opt-in `inductive` gate uses. Their doctor rows `goto-cc (inductive gate)`, `goto-instrument (inductive gate)` and `cbmc release pin (inductive gate)` are not required checks, so they never cause exit 30: they report whether both tools are found and whether `cbmc`, `goto-cc` and `goto-instrument` all print the tested release token `(cbmc-5.95.1)`. Without that, the `inductive` gate is `ERROR`.

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
| project root | nearest ancestor directory that contains `leanc.yaml`, otherwise the spec's directory; `--project-root` overrides it. `spec draft` has no spec and uses the current directory unless `--project-root` is given |
| spec (`spec.yaml`) | trusted contract: target signature, contract, sources, compile/analysis/test/CBMC settings, required `gates`. Schema: [docs/spec-format.md](docs/spec-format.md) |
| protected files | the spec, `sources.headers`, `verification.harness`, `tests.driver`, `inductive.harness`, and V2 ACSL headers and Lean spec/vectors. Generation and repair never write them |
| editable files | `sources.editable`, plus `lean.proofs` and `inductive.contracts` when the spec has those sections: the only files that repair may write (`generate` writes `sources.editable` only). They are not hashed into the approval |
| approval | `leanc spec approve` binds the spec's semantic hash and the SHA-256 of every protected file into a record signed with a key in `LEANC_HOME` (default `$XDG_CONFIG_HOME/leanc`, or `~/.config/leanc` when `XDG_CONFIG_HOME` is not set). Any change → `SPEC_UNAPPROVED` until re-approved, and `version` must increase. A rollback ledger rejects approvals that have been replaced |
| spec draft | `leanc spec draft` writes `drafts/<id>/` marked `provenance: ai-draft`. It is never trusted until a human reviews and approves it |
| gate | one verifier stage: `compile`, `integrity` (mandatory; runs after a passing compile gate), `static_analysis`, `sanitizer`, `cbmc`, the opt-in `inductive`, and in V2 `wp` and `lean`. Success requires every required gate to run and pass |
| run | every `generate` / `verify` / `repair` / `run` creates `.leanc/runs/<run_id>/`. `spec draft` creates `.leanc/runs/draft-<UTC timestamp>-<hex>/` for its `leanc.log` and the `llm/` transcripts of a real provider; unless the command aborts (see exit 2) it writes no `run.json` there, so `report latest` skips it |

Rule for any agent: never edit protected files, and never run `spec approve` on a human's behalf. Approval is the trust boundary.

## Commands

| Command | Purpose | Creates a run |
|---|---|---|
| `leanc doctor [--no-sandbox]` | toolchain, sandbox and LLM configuration check (`--no-sandbox` skips the sandbox check) | no |
| `leanc init [DIR] [--template safe-array-get] [--force]` | new project from a template | no |
| `leanc spec draft --id ID (--requirement TEXT \| --requirement-file F) [--title T] [--force]` | requirement → untrusted draft in `drafts/<id>/` (exit 40) | log directory only |
| `leanc spec approve SPEC --by NAME [--note N] [--yes]` | human approval (hash-lock + sign) | no |
| `leanc spec status SPEC` | approval state, signature validity, changes since approval (exit 40 when not approved) | no |
| `leanc spec show SPEC` | normalized spec, hash, effective CBMC checks | no |
| `leanc generate SPEC [--force]` | write `sources.editable` for an approved spec (exit 0 = generated, **not** verified; the recorded `status` is `UNKNOWN`) | yes |
| `leanc verify SPEC` | run the verifier gates on the current sources | yes |
| `leanc repair SPEC [--max-rounds N] [--no-write]` | verify, then a counterexample-driven repair loop (`--max-rounds`: 0 to 20) | yes |
| `leanc run SPEC [--regenerate] [--no-repair] [--max-rounds N] [--no-write]` | spec check → generate (if missing) → verify → repair → re-verify → report (`--max-rounds`: 0 to 20) | yes |
| `leanc report [RUN\|latest] [--format md\|json]` | show a run's report; exits with that run's status, or 20 (STALE) if files changed after a successful run; a `generate` run with status `UNKNOWN` (generated, not verified) exits 0 | no |

Common options:

| Option | Commands | Effect |
|---|---|---|
| `--json` | all except `report` | single JSON object on stdout; logs go to stderr |
| `-v` | all except `report` | verbose log to stderr |
| `--project-root DIR` | spec, generate, verify, repair, run, report | override project root |
| `--gates a,b` | verify, repair, run | run a subset of the spec's gates (result at most `UNKNOWN`) |
| `--fail-fast` | verify, repair, run | stop after the first gate that does not pass |
| `--timeout-scale X` | verify, repair, run | multiply every tool timeout (0.01 to 100) |
| `--no-sandbox` | verify, repair, run | do not use bwrap; dynamic tests are `SKIPPED` → at most `UNKNOWN` |
| `--allow-unsandboxed` | verify, repair, run | DANGEROUS: run untrusted test programs without a sandbox (only this flag can do it; `leanc.yaml` cannot) |
| `--check-unapproved` | verify, repair, run | run the gates for information on an unapproved spec; the status stays `SPEC_UNAPPROVED` |
| `--provider mock\|openai`, `--mock-dir D`, `--llm-timeout S` | spec draft, generate, repair, run | LLM selection |

## Statuses and exit codes

| exit | status | meaning |
|---|---|---|
| 0 | `PROVED` | required `wp` gate proved the approved ACSL contract and all RTE guards (smoke tests clean), and WP applies every semantic `compile.flags` entry and the build's `char` signedness. Only source of `PROVED` |
| 0 | `VERIFIED_INDUCTIVE` | opt-in: everything `VERIFIED_BOUNDED` needs, plus a required `inductive` gate that passed. CBMC with DFCC loop contracts showed that every execution of `main()` of the protected inductive harness as written, with at most one call of a claimed function (`inductive.functions`) per path, satisfies the harness assertions that execute on it and the mandatory check classes only, with every contracted loop proved by induction (invariant at entry and after one step, assigns, decreases); non-const static objects start arbitrary and allocations succeed. The harness's `LEANC_ASSUME`s are only an upper bound of the covered states (its other code narrows them too, unreported); functions that are not claimed and the extra `verification.checks` classes have only the bounded result; the known gaps are under [Limitations](#limitations). **Never promoted to `PROVED`** |
| 0 | `VERIFIED_BOUNDED` | no violation within the approved harness, input bounds and unwind bound; all loops fully unwound; harness non-vacuous. **Never promoted to `PROVED`** |
| 10 | `COMPILE_ERROR` | compiler errors (or warnings with `warnings_policy: block`) |
| 11 | `STATIC_ANALYSIS_WARNING` | analyzer finding with `policy: block`, or an integrity-gate violation |
| 12 | `RUNTIME_TEST_FAILED` | sanitizer report, a wrong exit code or output, or a Lean test-vector mismatch |
| 13 | `COUNTEREXAMPLE_FOUND` | CBMC found a concrete counterexample (a real execution path, valid even with an insufficient unwind), or a Frama-C/WP prover reported a proof goal invalid (that failure has the goal's location but no inputs or trace). The `inductive` gate never reports a counterexample |
| 20 | `UNKNOWN` | inconclusive: unwind too small, vacuous harness, skipped gate, gate subset, no formal gate (C++), unproved goals, a WP proof under `compile.flags` or a build target `char` signedness that Frama-C does not apply, an `inductive` gate that is `INCONCLUSIVE` (a loop without a contract, a rejected or unproved loop contract, an unproved property, an unreached probe, an undeclared function) |
| 21 | `TIMEOUT` | a required tool timed out |
| 30 | `TOOL_ERROR` | a tool crashed or produced unparsable output, or the `inductive` gate is `ERROR` (CBMC tools of an untested release, a broken harness or trusted-symbol rule); also when the provider, once set up, fails during generation or the generated code is rejected (`generate`, `run`), when `run` cannot set up the provider for generation, or when a repair is needed and the provider cannot be set up (`repair`, `run`; the verification verdict stays in `initial_status` / `verification.status`, the reason is in `messages`) |
| 40 | `SPEC_UNAPPROVED` | no valid approval for the current spec and protected files |
| 50 | `REPAIR_EXHAUSTED` | repair loop ended without success; it leaves the workspace unchanged |
| 2 | — | usage or input error. If the command had already created its run directory, that run is recorded with status `TOOL_ERROR`, so `report` on it exits 30 |
| 70 | — | internal error (traceback on stderr) |
| 130 | — | interrupted (Ctrl-C) |

"No counterexample found" is not success. A timeout, a tool error, an unparsable result or a skipped gate never counts as success. A required `inductive` gate that does not pass makes the result at best `UNKNOWN`; when every required gate passes, a required `wp` gate gives `PROVED` rather than `VERIFIED_INDUCTIVE`. The aggregation rules are in [docs/status-semantics.md](docs/status-semantics.md).

## Gate rules

These rules close ways in which a gate could pass code that was not really checked; the gaps that remain are listed under [Limitations](#limitations). None of them can turn a failure into a success.

| Rule | Behaviour |
|---|---|
| integrity not passed | `sanitizer`, `cbmc`, `inductive`, `wp` and `lean` run only after integrity passes. Otherwise they are `SKIPPED` ("never executed, model-checked or proved"), also when `gates` has no `compile`, because integrity runs after a passing compile gate. `static_analysis` still runs. The status comes from integrity: `FAIL` gives `STATIC_ANALYSIS_WARNING`, `ERROR` gives `TOOL_ERROR`, `TIMEOUT` gives `TIMEOUT`; when integrity did not run, the status is `UNKNOWN`. In the repair loop, a candidate that makes integrity pass counts as progress even when the unblocked gates then find counterexamples, provided no gate that ran before (`compile`, `static_analysis`) loses its pass or gains a failure |
| stack frames | integrity compiles each editable file with `-fstack-usage` (first `-include` of an anchor function, then `compile.flags` minus warning controls, then `-O0 -fno-lto -fembed-bitcode=off`) and reads the frame sizes clang writes: a frame larger than 1 MiB is `STACK_FRAME_TOO_LARGE`, and a frame clang marks `dynamic` (stack allocated at run time: a VLA, `alloca`, `_alloca`) is `UNBOUNDED_STACK`. The anchor makes clang write a stack-usage file for every file it generates code for; if there is none (for example under `-emit-llvm`) or the compile does not exit 0, integrity is `ERROR`. The VLA / `alloca` check (`UNBOUNDED_STACK`) compiles each file three times. The first run uses `compile.flags` and fails closed: it passes only when every error line is a parsed VLA / `alloca` diagnostic. The second turns the system-include options of `compile.flags` (`-isystem`, `-idirafter`, `-iwithsysroot`, ...) into `-I` placed after the spec's include directories, in their `compile.flags` order, and drops `--system-header-prefix=`; an `-idirafter` directory is then searched before the toolchain's own headers, so this run can fail to compile where the build does not. The third runs without `compile.flags`, as earlier versions did, and also fails closed, so a file that compiles only with a `compile.flags` entry is `ERROR`: put include directories in `sources.include_dirs` and macros in `compile.defines`. Only the second run adds violations without failing: when it does not compile, the VLA / `alloca` diagnostics it printed still count as violations and its other error lines are ignored; a timeout, or a compiler that cannot be run, still counts there. So system-include options, module maps and pragmas in forced includes cannot hide a VLA that the check without them reports, while the compiler's own system headers (libstdc++) stay system headers |
| trigraphs | a trigraph (`??/`, `??=`, ...) anywhere in editable C code, comments and string literals included, is `TRIGRAPH`: clang in ISO C modes (`-std=c17`) and Frama-C (its gcc runs with `-std=c11`) replace trigraphs, while CBMC preprocesses in gcc's gnu modes without them, so a `??/` that ends a comment hides the next line from one side only. A raw string prefix (`R"`, `u8R"`, `LR"`, ...) is `TOOL_DIVERGENCE`: gcc's gnu modes read a raw string literal, clang in C reads an identifier and a string. `__FILE__` is `TOOL_DIVERGENCE` too, because `-fmacro-prefix-map=` changes it for clang only |
| type selection and hidden calls | integrity rejects, in editable code and after macro expansion (so a macro of a protected header is caught where editable code uses it): `_Generic`, `__builtin_types_compatible_p` and `__builtin_classify_type` as `TOOL_DIVERGENCE` (CBMC 5.95.1 and clang can select different associations: `_Generic(x, long: 1, long long: 2)` with `long x` is 1 under clang and 2 under CBMC), the `mode` attribute as `TOOL_DIVERGENCE` (CBMC ignores it: a `mode(QI)` type is 1 byte natively and 4 bytes to CBMC) and the `cleanup` attribute as `CONSTRUCTOR` (neither CBMC nor Frama-C calls the cleanup function, so its accesses are never checked) |
| self-loops (CBMC) | the verify and vacuity runs pass `--no-self-loops-to-assumptions` after `--unwinding-assertions`. Without it CBMC turns a loop that jumps to itself (`for (;;) {}`, `while (1);`, `L: goto L;`, `while (k > K) {}`) into an assumption and drops the paths that reach it, so code that hangs natively on an admitted input would be `VERIFIED_BOUNDED`; with it such a loop fails its unwinding assertion (`INCONCLUSIVE`, status `UNKNOWN`) |
| vacuity build (CBMC) | before any CBMC run, every editable source and the harness are preprocessed once (`cbmc --preprocess`, with `SOURCE_DATE_EPOCH=0`, so `__DATE__` and `__TIME__` are fixed) without `-DLEANC_VACUITY_CHECK`, and once with it when a vacuity run follows. The texts are written to `build/cbmc/verify/` and `build/cbmc/vacuity/` (`<path>.i`), and the verify, coverage and vacuity runs read only those files, so every run checks the text that was compared, whatever the harness reads from the clock or the file system (`__has_include` of a run output). The two texts of a unit must be identical once each `LEANC_REACH` expansion in code position (`__CPROVER_assert(0, "leanc_reach:" "label")` on one line, not text inside a string literal, not called through `.` or `->`) is replaced by `((void)0)`. Otherwise the gate is `ERROR` (`TOOL_ERROR`) and no CBMC run is started: a harness that branches on `LEANC_VACUITY_CHECK`, directly or through a stringized probe, would show that another program reaches its probes than the one verified. Every such site of the vacuity text needs its own reported probe at its file, line and label (an aliased probe in code CBMC drops is not reported); a site without one is a vacuous harness (`INCONCLUSIVE`, status `UNKNOWN`) |
| `__CPROVER_` names (CBMC) | after the verify run, `cbmc --show-symbol-table` of the preprocessed units (the vacuity units when a vacuity run follows, since a name used only at a probe site is dropped from the verify units as unused) must contain no symbol with a `__CPROVER_` name (function, parameter, local, global, typedef; the base name or the last `::` part) located in a source file. A parameter `void (*__CPROVER_assert)(int, const char *)`, a local of that type or a definition of `__CPROVER_assert` would make `LEANC_REACH`, `LEANC_ASSUME` and the checks Lean C inserts call harness code; such a program is `ERROR` (`TOOL_ERROR`). The inductive gate applies the same rule to the linked symbol table of both of its builds. A block-scope redeclaration of a built-in is rejected too |
| predefined macros | only for specs with `cbmc` or `wp`. Clang's predefined macros under the compile gate's options (`-std`, `compile.flags`, `compile.defines`) are compared with gcc's under each verifier's own preprocessing. CBMC: `-D__CPROVER__ -m64`, `-funsigned-char`, `-std=gnuNN`, the `-D` defines it receives, then its x86-64 defines (`__LP64__`, `__x86_64__`, ...), in the order CBMC runs gcc. Frama-C: `-undef -nostdinc` with the machdep's builtin macros (`frama-c -print-machdep-builtin-macros`, which define `unix` and `linux`), `-D__FRAMAC__ -std=c11` and `compile.defines`. A use of a differing macro in editable code is `TOOL_DIVERGENCE`. Needs `gcc`, and `frama-c` for `wp`; if one is missing, integrity is `ERROR` |
| `compile.flags` | every entry is one option with its value joined to it (`-Iinclude`, `-DNAME=1`, `-std=c17`, `-includepre.h`). The spec validator rejects input files, `-` and `--`. The integrity gate asks clang (`clang -### <entry>`) whether an entry takes the next entry as its value (`-I -Wno-such-dir`, `-iprefix -funsigned-char`); such an entry is a `TOOL_ERROR`, because the CBMC translation and the stack checks read every entry as an option of its own. Also rejected: `-fsanitize*` / `-fno-sanitize*` (sanitizers come from `tests.sanitizers`), and options that hand arguments to an internal tool or replace the toolchain: `-X...` (`-Xclang`, `-Xanalyzer`, `-Xlinker`, ...) / `--for-linker`, `-mllvm`, `-Wp,` / `-Wa,` / `-Wl,`, `--analyze...`, `@file`, `--config`, `-fplugin` / `-fpass-plugin`, `-ccc-*`, `-B` / `--prefix`, `--ld-path` / `-fuse-ld=<path>`, `-resource-dir` / `--resource-dir`, `--sysroot` / `-isysroot`, `-ivfsoverlay` / `-vfsoverlay`, `--gcc-toolchain` / `--gcc-install-dir`, `-nostdlib` / `--no-standard-libraries` / `-nodefaultlibs` / `-nostartfiles`, `--driver-mode`. Options that change what the preprocessor prints or stop before code generation are rejected as well, because integrity reads the preprocessed source with `compile.flags`: `-E`, `-P` / `--no-line-commands`, `-C` / `-CC`, `-d*` (`-dM`, `-dD`, ...), `-M` / `-MM`, `-fdirectives-only`, `-frewrite-includes`, `-fuse-line-directives`, `-fkeep-system-includes`, `-fminimize-whitespace`, `-traditional-cpp`, `-fsyntax-only`, `-fdriver-only`; preprocessed output without a linemarker for the file is `ERROR`. Options that change how C source is split into tokens are rejected too, because CBMC preprocesses in gcc's gnu modes (up to gnu11) and Frama-C in gcc's `-std=c11`: strict C89/C90 (`-std=c89`, `-std=c90`, `-std=iso9899:1990`, `-std=iso9899:199409`, `-ansi`), which has no `//` comments, C23 (`-std=c23`, `-std=c2x`, `-std=gnu23`, `-std=gnu2x`), which has digit separators, and `-fdigraphs` / `-fno-digraphs`. `-Wwrite-strings` is rejected: clang turns it into `-fconst-strings`, which gives string literals the type `const char[N]`, and no verifier applies that. Static analysis receives the remaining flags. CBMC receives `-D`, `-I`, `-std=` / `--std=` and `-funsigned-char` (signed `char` is CBMC's x86 default), also in their GCC long spellings (`--define-macro=`, `--undefine-macro=`, `--include-directory=`, `--unsigned-char`, `--signed-char`), but never `NDEBUG`. A `-U` only cancels an earlier `-D`, also of a function-like macro; any other `-U` is reported as not applied. Warning and `-g` flags have no semantic effect, except `-Wdeprecated` / `-Wno-deprecated`, which decide `__DEPRECATED` in C++ and are kept for the stack checks. Every other flag (for example `-O2 -ffast-math -mavx2`) is listed in the assumption line "compile.flags not applied by CBMC"; the CBMC verdict is kept. For WP, each entry is applied, has no semantic effect (warning and `-g` flags, as above) or is not applied. Char signedness counts as applied only when the build's `char` signedness equals that of Frama-C's machdep (signed on `x86_64`). The build's signedness is the last char-signedness entry (`-fsigned-char`, `-fno-unsigned-char`, `--signed-char` are signed), or else the default of the build target that `clang <compile.flags> -dumpmachine` prints: signed for the architectures `x86_64`, `i386`, `x32` (the table CBMC uses), not known to be signed for any other. With no char flag, a target not known to be signed (for example `aarch64-unknown-linux-gnu`) adds the not-applied entry `(default char of build target ...)`; a failed query or an unparsable triple adds `(build target unknown: ...)` whatever the flags. A `-std=` / `--std=` entry is applied when the last one selects C11 or C17 (`c11`, `c1x`, `iso9899:2011`, `c17`, `c18`, `iso9899:2017`, `iso9899:2018`); it is disclosed like `language.standard`, as "Frama-C preprocesses and parses the code as c11". `gnu*` and other standards are not applied. Every other entry is not applied, including `-D`, `-U` and `-I` (Frama-C receives only `sources.include_dirs` and `compile.defines`). Not-applied entries are listed in "compile.flags not applied by Frama-C" (`details.compile_flags` of the `wp` stage). A proof with any of them is the `wp` gate's `INCONCLUSIVE` (`UNKNOWN`), never `PROVED`, and the result under Frama-C's model stays in `details.frama_c_model_verdict`. Proof repair is not attempted then |
| body-less functions (CBMC) | allowed: `nondet_*`, `__CPROVER*`, `__builtin_prefetch`, `__builtin_expect`. Any other body-less callee keeps `cbmc` from passing: the SIMD builtins behind `<immintrin.h>`, and `memchr`, `strnlen`, `strstr`, `fputc` and `putc`, which integrity allows but CBMC has no model for. A violation whose trace uses the return value of such a callee is an `unmodelled-callee` diagnostic, not a counterexample; every other violation stays a counterexample. Without a counterexample the gate is `INCONCLUSIVE` (status `UNKNOWN`): replace the call with plain C to get a verdict |
| undeclared functions (CBMC) | CBMC types a call to an undeclared function as returning `int`, so an undeclared `nondet_*` input only takes `int` values converted to the caller's type. A `function 'f' is not declared` warning is an `undeclared-function` diagnostic and keeps `cbmc` from passing (`INCONCLUSIVE`, status `UNKNOWN`); violations stay counterexamples. `leanc.h` declares `nondet_int`, `nondet_uint`, `nondet_long`, `nondet_ulong`, `nondet_llong`, `nondet_size_t`, `nondet_char`, `nondet_uchar`, `nondet_short`, `nondet_bool`, `nondet_int32_t`, `nondet_uint32_t`, `nondet_int64_t`, `nondet_uint64_t`: declare any other `nondet_*` in the harness |
| CBMC internal error | an invariant-violation report is `TOOL_ERROR` ("CBMC internal error") with the File/Condition/Reason excerpt, not an out-of-memory hint |
| contract safety checks | the checks in `contract.safety` are added to CBMC's property checks; the spec hash is unchanged |
| sanitizer builds | the test, ownership-trace and Lean vector builds add `-fno-builtin -fno-optimize-sibling-calls` after `compile.flags`, so clang `-O1` cannot delete unused allocations (`malloc`, `strdup`, `aligned_alloc`, C++ `new`) or the frames of wrapper functions |
| memory limitations | `limitations` lists what the leak checks could not see; a check counts only when its gate reached a verdict (`PASS` or `FAIL`). LeakSanitizer reports only unreachable blocks. Without a CBMC `memory-leak` verdict, leaks are checked only on the test paths. CBMC does not check alignment, strict aliasing, `restrict` or overflows between sub-objects of one allocation |
| `inductive` gate (opt-in) | runs after `cbmc` for a spec whose `gates` list `inductive`; the spec validator then requires the `inductive` section, the `cbmc` gate and `verification.malloc_may_fail: false`. Like `cbmc`, `wp` and `lean`, it runs only after integrity passes and after the protected files are checked against the approval again. It needs `cbmc`, `goto-cc` and `goto-instrument` (`LEANC_CBMC`, `LEANC_GOTO_CC`, `LEANC_GOTO_INSTRUMENT`) whose version lines each carry exactly one `(cbmc-X.Y.Z)` token, all of the same tested release (5.95.1 only), with `goto-cc` in its gcc personality (`gcc (goto-cc ...`); anything else is `ERROR`. Every tool run of the gate (preprocessing, goto-cc, goto-instrument, the two CBMC runs) has the limits `verification.timeout_s` × `--timeout-scale` and `verification.memory_mb`; a timeout is `TIMEOUT`, never a partial pass. The target function must be claimed, which can make the gate slow (about 70 s for `examples/inductive_sponge`) |
| inductive harness | `inductive.harness` is protected. Its `main()` (defined once, in that file) assumes the state invariant with `LEANC_ASSUME` on an arbitrary state, calls the claimed functions directly, asserts after the calls and contains at least one `LEANC_REACH`. Lean C instruments its own copy, and each of the following is `ERROR`: a path that reaches a second claimed call from the harness after an earlier one (checked per path by an inserted assertion, so no claimed function starts from another one's post-state); a `LEANC_ASSUME` or `__VERIFIER_assume` after a claimed call (in the same function, and per path), or a claimed call inside an assumption; a claimed function used other than in a direct call, or never called; a reference to `main` inside a function body; any function of the editable sources entered before the claimed function (an editable predicate in an assumption, a setup call, an argument of the claimed call: every editable function gets an inserted entry check); a `return` in `main()` other than one final top-level `return`, or a `goto` in `main()`; an `=`, compound assignment, `++` or `--` in `main()` after its last `LEANC_ASSUME` (or its start) up to the end of the first claimed call, except the `=` of a plain declaration `T x = ...;`; a use of `exit`, `abort`, `_Exit`, `quick_exit`, `longjmp`, `raise`, `err`, `errx`, `verr`, `verrx` or another name that ends or leaves a path, in the harness unit or its headers, even as a function pointer value; a `leanc_` name; a read of an editable object whose initialiser the inductive run keeps (const, or with const parts); an `#include` of an editable source; preprocessings with and without `-DLEANC_VACUITY_CHECK` that differ in more than the `LEANC_REACH` expansions. Not detected: other harness code narrows the covered states too (declaration initialisers, writes before the last `LEANC_ASSUME`, writes after the first claimed call, branches around the assertions, helper functions), so the approver must review the whole harness |
| inductive build | the gate works only on the `cbmc --preprocess` output of each editable unit, of the harness (with and without `-DLEANC_VACUITY_CHECK`) and of its copy models, with `SOURCE_DATE_EPOCH=0`. The clauses of `inductive.contracts` must pass a strict grammar (no calls, assignments, `++` / `--`, `?:`, comma, address-of, `sizeof`, quantifiers, or macro names defined in `sources.editable` or standard ones such as `NULL` and `INT_MAX`; see [docs/spec-format.md](docs/spec-format.md)), and only their fully parenthesised rendering is inserted, at the one `for` / `while` loop spelled on the contract's line with a `{` body. Any other site, or a clause that does not compile (for example a protected-header macro name, which is not expanded there), is `contract_rejected` (`INCONCLUSIVE`). `goto-cc --verbosity 2` compiles the trusted units first (the copy models, then the harness) and the editable units after them. In the linked symbol table every trusted symbol must stay bound to its own definition, no `<trusted name>$link<N>` rename may exist (also between two statics: rename the editable one), no symbol of an editable file may be named `leanc_...` and no `__CPROVER_` name may be declared in a source file; otherwise `ERROR`. A goto-cc warning `function 'f' is not declared` is `undeclared_function` (`INCONCLUSIVE`). Then `goto-instrument --dfcc main --apply-loop-contracts` runs (never `--loop-contracts-file`, never without `--dfcc`); a loop outside the harness and the CBMC library that is still uncontracted is `loop_without_contract` (`INCONCLUSIVE`), and CBMC is not run. `memcpy`, `memmove` and `memset` are replaced by models that check the source and destination ranges (and, for `memcpy`, no overlap) and then make the destination contents arbitrary; non-const static objects start arbitrary; allocations never fail |
| inductive verdict | the verify run is `cbmc dfcc.goto --no-library <checks> --unwind U --unwinding-assertions --no-self-loops-to-assumptions [--object-bits N] --json-ui`, with U = max(`verification.unwind`, longest assigns list + 2) and only the mandatory check classes (`bounds`, `pointer`, `div-by-zero`, `signed-overflow`, `pointer-overflow`, `undefined-shift`): from an arbitrary state, legitimate modular arithmetic and leaks are meaningless, so the `verification.checks` and `contract.safety` classes keep only the bounded result. Every contract must map to exactly one CBMC loop with all five classes `loop_invariant_base`, `loop_invariant_step`, `loop_decreases`, `loop_assigns` and `loop_step_unwinding` (otherwise `ERROR`); a contract without any of them (its loop is not reached from the harness) is `contract_rejected`. The vacuity run must reach every `LEANC_REACH` site, a `leanc_function:<f>` probe at the entry of each claimed function, a `leanc_loop:<file>:<line>` probe at the start of each contracted loop body and a `leanc_return:<f>` probe right after each claimed call; an unreached probe is `vacuous_harness`. A property that does not hold gives `INCONCLUSIVE` with a reason code: `inductive_step_failed`, `suspected_non_termination` (the decreases clause, at the loop), `unwind_insufficient`, `unmodelled_callee` or `property_unproved`. These failures have kind `unproved` and are never counterexamples, because each claimed call and each loop starts from an arbitrary state that real executions may never reach. An undecided property, a broken harness rule or harness assertions missing from CBMC's property list are `ERROR`. The gate ends `PASS`, `INCONCLUSIVE`, `TIMEOUT` or `ERROR`, never `FAIL`. A required `PASS` together with `cbmc` `PASS` gives `VERIFIED_INDUCTIVE`, and the scope it states is listed in `assumptions` |

## Machine interface (AI agents / CI)

Use `--json` and branch on the exit code or on `status`. With exit 2, 70 or 130, and when the interactive confirmation of `spec approve` is declined (exit 40), no JSON object is printed; the message is on stderr. A successful `generate` reports `status` `UNKNOWN` with `exit_code` 0. `leanc verify SPEC --json` prints:

```json
{
  "command": "verify", "status": "COUNTEREXAMPLE_FOUND", "exit_code": 13,
  "run_id": "verify-20261121T034210Z-7c8afd", "run_dir": ".../.leanc/runs/verify-...",
  "provider": null, "report": ".../report.md", "messages": ["..."],
  "verification": {
    "status": "...", "success": false, "spec_id": "...", "spec_version": 1, "spec_hash": "...",
    "approval": {"...": "..."},
    "stages": [{"stage": "cbmc", "outcome": "FAIL", "status": "...", "required": true, "invoked": ["cbmc"],
                "duration_s": 1.2, "summary": "...", "tools": ["cbmc"], "failures": 3}],
    "detected_by": ["asan", "cbmc"], "reasons": ["..."], "assumptions": ["..."], "limitations": ["..."],
    "reason_codes": [{"code": "gate_failed", "stage": "cbmc", "location": null, "detail": "..."}],
    "failures": [{
      "id": "sanitizer:index_equals_count:asan:heap-buffer-overflow", "tool": "asan", "stage": "sanitizer",
      "kind": "...", "property_id": "...", "location": {"file": "src/array_lookup.c", "line": 9, "column": 16, "function": "array_lookup"},
      "inputs": [{"name": "argv", "value": "index_equals_count", "type": null}],
      "description": "...", "raw_ref": "raw/...", "ownership_trace": null
    }]
  }
}
```

`reason_codes` (also in `verification.json`, and listed under "Reason codes" in `verification.md` and `report.md`) is empty for a success status. Otherwise it explains the status by the required stages that did not pass (for `SPEC_UNAPPROVED`, by the spec check); each entry has `code`, `stage`, `location` (or `null`) and `detail`. An `INCONCLUSIVE` stage with specific codes lists only those; any other outcome first gives its generic code (`gate_failed`, `gate_skipped`, `gate_timeout`, `tool_error`, `gate_inconclusive`). The specific codes are `unwind_insufficient`, `vacuous_harness`, `vacuity_unchecked`, `unmodelled_callee`, `undeclared_function`, `property_unproved` and `frama_c_model_only_proof`, and from the `inductive` gate also `inductive_step_failed`, `suspected_non_termination`, `contract_rejected` and `loop_without_contract`; `no_formal_gate` marks an `UNKNOWN` spec without a `cbmc` gate or a required `wp` gate. Branch on `status` and `exit_code`; the codes say why.

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

The repair prompt shows the same evidence with an explicit label. It does not identify which `return` statement ran. The code under test runs in the traced process, so it can forge or suppress this evidence (for example by appending lines to the trace log through its inherited file descriptor, or by printing probe markers or LeakSanitizer-shaped lines on stderr): the evidence can then be wrong or missing, but the verdict is not affected.

There are two ways for an agent to fix code:

1. **External agent loop.** Run `leanc verify SPEC --json`. Read `verification.failures` (location, inputs, property) and `raw_ref`. Edit only the files listed in `sources.editable` (and `inductive.contracts` or `lean.proofs` when the spec has them), then verify again. Stop on exit 0, or on 40 (needs a human), 20/21/30 (not a code bug: inspect `reasons` and `reason_codes`; `loop_without_contract`, `contract_rejected` and `inductive_step_failed` ask for work on `inductive.contracts`).
2. **Built-in loop.** Run `leanc run SPEC --provider openai --json` (or `repair`). Lean C calls the model, policy-checks every patch and writes back only a candidate for which every required gate passes: a verified one (`VERIFIED_BOUNDED` / `VERIFIED_INDUCTIVE` / `PROVED`) or, for a spec with neither a `cbmc` nor a `wp` gate, one whose status stays `UNKNOWN` (`--no-write` does not write the repaired candidate back; in `run`, generating missing sources, or all of them with `--regenerate`, still writes `sources.editable`).

Repair policy rejects these patches: writes outside the editable files, `__CPROVER_*` / `LEANC_*` / `__builtin_assume`, deleting `assert`, suppressing warnings or sanitizers, `#include` escapes, `system`/`exec`/`fork`/`socket`, inline asm, hollowing out the target function, and oversized patches. Repeated patches are detected as cycles. A change toward `UNKNOWN`/`TIMEOUT` is not an improvement. With the `inductive` gate, the contracts file must pass the clause grammar (`LOOP_CONTRACT`); in a round where only proof gates are open and `inductive` is one of them, a change to any other file is rejected before verification (`CONTRACTS_ONLY_ROUND`); and when the `inductive` gate did not pass at the start of the session, a candidate whose C code differs from the session's starting sources and that passes the `inductive` gate is rejected after verification (`INDUCTIVE_CODE_EDIT`): it is never accepted or written back, and its sources stay in `repair/round-<n>/blocked-candidate/` for a human. The `inductive` stage's reason codes count alongside failures when repair measures progress.

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
| `LEANC_ALLOW_INSECURE_HTTP=1` | allow sending the API key over plain `http://` to a host that is not loopback. Without it such a `LEANC_BASE_URL` is refused whenever a key is set (`OPENAI_API_KEY` counts); `localhost`, `*.localhost`, loopback IPs and endpoints without a key are always allowed |
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

The project config cannot weaken checks, bounds, gates or approval, and it cannot make untrusted programs run without a sandbox. `sandbox: none` acts like `--no-sandbox`: bwrap is not used for the tools either, and dynamic tests are `SKIPPED`. If it sets `allow_unsandboxed`, that value is ignored with a warning.

| Variable | Meaning |
|---|---|
| `LEANC_HOME` | approval key and ledger directory (default `$XDG_CONFIG_HOME/leanc`, or `~/.config/leanc` when `XDG_CONFIG_HOME` is not set); keep it outside the workspace |
| `LEANC_CLANG`, `LEANC_CLANGXX`, `LEANC_CBMC` | tool paths |
| `LEANC_GOTO_CC`, `LEANC_GOTO_INSTRUMENT` | `goto-cc` and `goto-instrument` for the `inductive` gate; otherwise `PATH` is searched. Together with `cbmc`, they must report the same tested release (5.95.1) |
| `LEANC_FRAMAC` | `frama-c` (>= 27, with WP); otherwise `PATH` and `/opt/opam/*/bin` are searched |
| `LEANC_LEAN` | `lean` (toolchain with `leanchecker`); otherwise `PATH` and `/opt/lean/*/bin` are searched |

## V2 optional tools

| Feature | Install | Spec | Status effect |
|---|---|---|---|
| Frama-C/WP + ACSL | `sudo apt-get install -y opam z3 cvc5 && opam init -y && opam install -y frama-c alt-ergo` (Ubuntu's `frama-c-base` has no WP) | ACSL contract in a protected header, gate `wp` | only route to `PROVED`; proof repair lets the AI add loop invariants |
| Lean 4 | release toolchain unpacked to `/opt/lean/<version>` or via elan | protected model and theorem statements; the AI writes proofs; kernel + `leanchecker`; standard axioms only; differential test vectors | model level: unproved → `UNKNOWN`, vector mismatch → `RUNTIME_TEST_FAILED`; never `PROVED` |
| C++20 | `clang++` (part of clang-18) | `language: {name: cpp, standard: c++20}` | no C++ model checker is available, so the best result is `UNKNOWN` |
| Inductive profile (opt-in, C) | nothing beyond `cbmc`: Ubuntu 24.04's package is CBMC 5.95.1, the only tested release, and ships `goto-cc` and `goto-instrument` | protected `inductive.harness`, editable `inductive.contracts`, `inductive.functions` (including `target.function`), gate `inductive` next to `cbmc` | `VERIFIED_INDUCTIVE` (exit 0) when `cbmc` passes too; never `PROVED`; an inconclusive proof is `UNKNOWN`, and repair may then change only the contracts file |

`leanc doctor` reports each of these tools. When a tool is missing, its tests are skipped rather than failed (a missing `goto-cc` or `goto-instrument`, or a CBMC release other than 5.95.1, fails the real-tool inductive tests instead of skipping them).

## Examples

Every `examples/<name>/` contains `spec.yaml`, protected `include/`, shipped (usually buggy) `src/`, `mock/` fixtures and `expected.yaml`. Examples with a `cbmc` gate also have a protected `harness/` (the two C++ examples have none), and examples with a `sanitizer` gate have protected `tests/` (`vacuous_harness` has none). `inductive_sponge` also has the protected inductive harness `harness/sponge_inductive.c` and the editable contracts file `inductive/loops.json`.

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
| `inductive_sponge` | correct; the opt-in `inductive` gate claims `sponge_absorb` for every length up to 2^48 | `VERIFIED_INDUCTIVE` | `VERIFIED_INDUCTIVE` (no repair round) |

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
- No comments or docstrings in `leanc/`, `tests/` or `scripts/`, except in a `fixtures/` directory under `tests/` or `scripts/`. Express intent through names, constants, Enums and frozen dataclasses.
- No bare/`Exception`/`BaseException` handlers (the only exception is `except Exception` / `except BaseException` in `leanc/cli.py` `main()`, which exits 70; a bare `except:` is rejected there too) and no swallowed exceptions.
- Every `.py` file has at most 600 lines (same `fixtures/` exception).

| Package | Role |
|---|---|
| `foundation` | statuses, models, reason codes, inductive scope texts, errors, hashing, redaction, workspace (path/symlink safety), sandbox, `run_command` (argv only, limits) |
| `specs` | spec schema, loading, hashing, approval and ledger |
| `lang` | C / C++ / Lean lexers, the loop-clause grammar |
| `rules` | pure trust rules shared by gates and repair policy (C, C++, ACSL, Lean, preprocessor, loop contracts and loop sites) |
| `toolchain` | tool discovery, the CPROVER release pin, `BackendContext` |
| `reports` | JSON / Markdown rendering |
| `llm` | provider interface, mock, OpenAI-compatible, prompts, response parsing |
| `backends` | gates: clang, analyzer, integrity, sanitizer, cbmc, inductive, counterexample, wp, lean |
| `repair` | patch application (fuzz 0), generation and repair policy |
| `pipeline` | verification controller, reason codes, generation, repair loop and its inductive guard, doctor, config, command bodies |
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
6. Check approval compatibility: for every example directory unchanged since the base, an approval made with the base code must still be valid under the HEAD code. At least one example directory must be unchanged, otherwise the step fails.
7. Confirm the worktree is still at the pinned HEAD and clean.

On success it prints `MERGE GATE PASS head=<sha> base=<sha>`. Right before merging, fetch and confirm that the remote base still equals that base sha (otherwise run the gate again), then merge only that head sha (`expectedHeadSha`, merge commit). Any new commit requires running the gate again.

The full suite needs clang-18, gcc, CBMC 5.95.1 with its `goto-cc` and `goto-instrument` (the real-tool inductive tests run them; a missing goto tool or another release fails those tests, never skips them), bwrap, llvm-symbolizer under /usr/ (package llvm-18) and an x86-64 CPU with AVX2 (SIMD tests execute AVX2 code). WP tests, and the integrity tests of `wp` specs, need Frama-C >= 27 with `-print-machdep-builtin-macros` (tested with 33); Lean tests need Lean 4. When a tool is absent its tests are skipped, so the merge gate needs all of them.

## Limitations

- `VERIFIED_BOUNDED` is bounded to the approved harness. A human still has to judge whether the harness captures the requirement.
- `VERIFIED_INDUCTIVE` is relative to the protected inductive harness as written: it covers the executions of its `main()` with at most one claimed call per path, for the mandatory check classes and the harness assertions only. The harness's `LEANC_ASSUME`s are an upper bound of the covered states; declaration initialisers, writes before the last `LEANC_ASSUME` or after the first claimed call, branches around the assertions and helper functions narrow them further without being reported. A weak assertion gives a weak claim: `examples/inductive_sponge` asserts only `rc == SPONGE_OK` and `s->fill < SPONGE_RATE`, so code that silently absorbs only part of a long input would not be caught there.
- Known gaps of the `inductive` gate: a pointer access that overflows a member array into a neighbouring field or the padding of the same object (`p->a[i]`, pointer arithmetic, `q = p->a; q[i]`, the copy models) and a copy into a string literal are not reported (the bounded `cbmc` gate has the same gap; `s.a[i]` on a named object is checked). Calls of CBMC library models that contain loops (`strlen`, `strcpy`, `strncpy`, `strncat`) fail the loop-contract write-set checks, so such code never passes the gate (incomplete, never a false pass).
- The repair rules of the `inductive` gate (`CONTRACTS_ONLY_ROUND`, `INDUCTIVE_CODE_EDIT`) restrict only the repair agent: code written by a person that gives up correctness at large sizes can still be `VERIFIED_INDUCTIVE` under a weak harness, and a correct code fix that makes the gate pass has to be applied by a person.
- Sanitizers only cover the paths that the trusted tests execute.
- CBMC does not model body-less functions (such as SIMD intrinsics) or flags it cannot apply (such as `-ffast-math`, `-O2`, `-m*`): the first make the result inconclusive, the second are disclosed as assumptions. WP proves the code under Frama-C's model: a semantic `compile.flags` entry that Frama-C does not apply (such as `-funsigned-char`, `-O2`, `-fwrapv`, `-D`) keeps the result from `PROVED`, so such a spec is at best `UNKNOWN`. A build target whose default `char` is not known to be signed (for example aarch64, but also arm64 Apple targets, whose `char` is signed) needs an explicit `-fsigned-char` for `PROVED`. Only `char` signedness is compared with the build target: other machine-model differences from Frama-C's `x86_64` machdep (type sizes, `long double`, alignment) are not checked. `language.standard` is disclosed, not checked: Frama-C parses C11, and another standard there does not block `PROVED`.
- The macro comparison sees only predefined macros used by name. A predefined macro name built by token pasting (`##`), and a macro defined by a system header that differs between the tools (for example `M_PI`, which glibc defines under `-std=gnu11` but not under `-std=c17`), are not compared: code that branches on them can make the compile gate and a verifier see different code.
- The `compile.flags` check is a list of rejected options, not a list of allowed ones.
- The frame-size check measures each function at `-O0`. Inlining at a higher `-O` can merge frames, and the depth of a call chain is not checked. A protected header that picks a function body by `__OPTIMIZE__` is measured in its `-O0` form (editable code cannot name `__OPTIMIZE__`). A protected header that marks itself as a system header (`#pragma GCC system_header`, or a linemarker with flag 3) hides its own VLA warnings, as before, and a diagnostic pragma in a protected header can switch VLA warnings off for the rest of the file; the frame check then sees only VLAs in functions emitted at `-O0`.
- `PROVED` holds only relative to the approved ACSL contract, under WP's memory and machine model.
- The repair policy is defense in depth (textual rules). The real defense is the protected harness plus independent verifiers.
- C++20 has no formal gate (CBMC cannot parse modern C++, and ESBMC is not integrated), so the best C++ result is `UNKNOWN`.
- Lean proves a model, linked to the C code only through differential test vectors.
- bubblewrap is not a VM. Run high-risk workloads inside a separate container or VM.

Further documentation (Traditional Chinese): [architecture](docs/architecture.md) · [spec format](docs/spec-format.md) · [status semantics](docs/status-semantics.md) · [trust model](docs/trust-model.md) · [security](docs/security.md) · [experiments](docs/experiments.md) · [roadmap](docs/roadmap.md)
