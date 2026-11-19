# 可重現實驗

```bash
python scripts/run_experiments.py                 # 全部範例
python scripts/run_experiments.py --only array_oob div_by_zero
```

對每個 `examples/<name>/expected.yaml`，腳本會：

1. 把範例複製到全新的暫存專案（不污染 repo）；
2. 以**暫時的** `LEANC_HOME` 金鑰批准規格——範例的規格、harness 與測試是人工撰寫的，
   腳本扮演人類審查者；你的真實金鑰不會被使用；
3. `leanc verify`：驗證範例附帶的（通常是有缺陷的）原始碼；
4. 在另一份全新複本執行 `leanc run --provider mock --regenerate`：mock 生成 → 驗證 → 反例驅動修復 → 再驗證；
5. 驗證 `expected.yaml` 中列出的規格變體（例如 `loop_unwind/spec_full_unwind.yaml`）；
6. 比對狀態、偵測工具（`detected_by`）與被政策拒絕的修復回合（`rejected_rounds`）。

輸出：`experiments/results/results.json`（含每個關卡的結果、耗時、工具版本、平台）與
`experiments/results/results.md`（表格）。所有指令都透過真正的 `leanc` CLI（`python -m leanc ... --json`）執行。

## 這些實驗量測什麼

* **缺陷偵測**：每個缺陷由哪個獨立工具發現（CBMC、Clang Static Analyzer、ASan、UBSan、LSan、測試 oracle）。
* **狀態誠實性**：unwind 不足、vacuous harness、超時都不會被報告為成功。
* **信任邊界**：修改 harness/規格、注入 `__CPROVER_assume`、刪除 assert、抑制警告、把函式掏空等作弊補丁被拒絕。
* **修復迴圈控制**：多回合修復、改善判定、循環偵測、達到上限時 `REPAIR_EXHAUSTED`。

**這些實驗不量測 LLM 的能力。** `--provider mock` 重播固定的 fixture；要評估真實模型，請以
`--provider openai` 執行並自行記錄模型、溫度與 prompt 版本（`leanc/llm/prompts.py`）。

## 測試案例對照

| 需求中的測試 | 範例 | 預期 |
|---|---|---|
| Test 1 正確程式 | `safe_array_get` | verify `VERIFIED_BOUNDED` |
| Test 2 陣列越界 | `array_oob` | verify `COUNTEREXAMPLE_FOUND`（輸入 `idx == len`）；run `VERIFIED_BOUNDED` |
| Test 3 除以零 | `div_by_zero` | verify `COUNTEREXAMPLE_FOUND`；兩回合修復後 `VERIFIED_BOUNDED` |
| Test 4 整數溢位 | `int_overflow` | verify `COUNTEREXAMPLE_FOUND`（強制 `signed-overflow` 檢查無法被移除） |
| Test 5 指標安全 | `null_deref` | Clang Static Analyzer **與** CBMC 都偵測到，報告區分工具 |
| Test 6 迴圈展開不足 | `loop_unwind` | `UNKNOWN`（不是成功、也不會被「修復」）；變體 unwind 17 → `VERIFIED_BOUNDED` |
| vacuous harness | `vacuous_harness` | 純 CBMC 說 SUCCESSFUL，Lean C 回報 `UNKNOWN` |
| 執行期記憶體錯誤 | `runtime_memory` | 只有 sanitizer 偵測到 → `RUNTIME_TEST_FAILED`；修復後 `VERIFIED_BOUNDED` |
| 多檔案專案 | `ring_buffer` | `VERIFIED_BOUNDED` |
| 修復作弊 | `cheating_repair` | 前 4 回合被政策拒絕，第 5 回合真正修復 |
| 修復耗盡 | `repair_exhausted` | `REPAIR_EXHAUSTED`，工作區不變 |
| 求解器超時 | `cbmc_timeout` | `TIMEOUT` |
| 規格竄改 | `tests/test_cli.py`、`tests/test_spec_and_approval.py` | 改 harness / 規格 / 批准紀錄 → `SPEC_UNAPPROVED` |
| 無沙箱 | `tests/test_cli.py::test_no_sandbox_blocks_dynamic_execution` | sanitizer `SKIPPED` → `UNKNOWN` |

## 最近一次結果

最近一次執行（2026-11-19T13:39:54Z，Ubuntu clang version 18.1.3 (1ubuntu1); cbmc 5.95.1 (cbmc-5.95.1)，Python 3.13.18，`run` 使用 **mock** provider——固定 fixture，**不是** LLM 表現）：

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

**12/12 範例符合預期。** 全部 `verify` 合計 38 s、全部 `run` 合計 60 s（4 核心容器）。
原始資料：[`experiments/results/results.json`](../experiments/results/results.json)。

另外，`tests/test_attacks_e2e.py` 收錄了對抗式審查中發現的 18 種「讓錯誤程式得到 `VERIFIED_BOUNDED`」的攻擊
（覆寫 `__assert_fail` / `nondet_*`、巨集組出的 `_Pragma(CPROVER check disable)`、`#ifdef __clang__`、
`exit`/`abort`/`atexit(_exit)`、linemarker、弱引用 `__VERIFIER_assume`…），現在每一種都會被強制的完整性關卡
擋下，且不會被執行。
