# Lean C experiment results

- date (UTC): 2026-11-19T14:13:04Z
- leanc: 0.1.0; python 3.13.18; Linux 6.18.44-fc-v653 x86_64
- tools: Ubuntu clang version 18.1.3 (1ubuntu1); cbmc 5.95.1 (cbmc-5.95.1)
- provider for `run`: **mock** (fixed fixtures; NOT a measurement of LLM performance)

| example | verify (expected) | detected by | run --provider mock (expected) | rounds | rejected rounds | match |
|---|---|---|---|---|---|---|
| array_oob | COUNTEREXAMPLE_FOUND (COUNTEREXAMPLE_FOUND) | asan, cbmc | VERIFIED_BOUNDED (VERIFIED_BOUNDED) | 1 | - | yes |
| cbmc_timeout | TIMEOUT (TIMEOUT) | - | TIMEOUT (TIMEOUT) | 0 | - | yes |
| cheating_repair | COUNTEREXAMPLE_FOUND (COUNTEREXAMPLE_FOUND) | asan, cbmc | VERIFIED_BOUNDED (VERIFIED_BOUNDED) | 5 | 1, 2, 3, 4 | yes |
| div_by_zero | COUNTEREXAMPLE_FOUND (COUNTEREXAMPLE_FOUND) | cbmc, ubsan | VERIFIED_BOUNDED (VERIFIED_BOUNDED) | 2 | - | yes |
| int_overflow | COUNTEREXAMPLE_FOUND (COUNTEREXAMPLE_FOUND) | cbmc, ubsan | VERIFIED_BOUNDED (VERIFIED_BOUNDED) | 1 | - | yes |
| loop_unwind | UNKNOWN (UNKNOWN) | cbmc | UNKNOWN (UNKNOWN) | 0 | - | yes |
| ↳ loop_unwind/spec_full_unwind.yaml | VERIFIED_BOUNDED (VERIFIED_BOUNDED) | | | | | |
| null_deref | COUNTEREXAMPLE_FOUND (COUNTEREXAMPLE_FOUND) | cbmc, clang-static-analyzer, ubsan | VERIFIED_BOUNDED (VERIFIED_BOUNDED) | 1 | - | yes |
| repair_exhausted | COUNTEREXAMPLE_FOUND (COUNTEREXAMPLE_FOUND) | cbmc, ubsan | REPAIR_EXHAUSTED (REPAIR_EXHAUSTED) | 5 | - | yes |
| ring_buffer | VERIFIED_BOUNDED (VERIFIED_BOUNDED) | - | VERIFIED_BOUNDED (VERIFIED_BOUNDED) | 0 | - | yes |
| runtime_memory | RUNTIME_TEST_FAILED (RUNTIME_TEST_FAILED) | lsan | VERIFIED_BOUNDED (VERIFIED_BOUNDED) | 1 | - | yes |
| safe_array_get | VERIFIED_BOUNDED (VERIFIED_BOUNDED) | - | VERIFIED_BOUNDED (VERIFIED_BOUNDED) | 0 | - | yes |
| vacuous_harness | UNKNOWN (UNKNOWN) | cbmc | UNKNOWN (UNKNOWN) | 0 | - | yes |
| ↳ vacuous_harness/spec_fixed_harness.yaml | COUNTEREXAMPLE_FOUND (COUNTEREXAMPLE_FOUND) | | | | | |

**12/12 examples match their expected statuses.**
