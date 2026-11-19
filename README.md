# Lean C

**規格優先、由獨立驗證器裁決的 AI C 程式碼生成研究原型**
*(Spec-first, verifier-checked AI code generation for C17 — research prototype)*

傳統流程：`需求 → AI 生成程式碼 → 編譯 → 輸出`。Lean C 的流程：

```
自然語言需求 → 形式化規格草案（不受信任）→ 人工確認（雜湊鎖定 + 簽章）
  → 程式碼生成 → Clang 編譯 → 原始碼完整性關卡 → Clang Static Analyzer → ASan/UBSan/LSan 可信測試
  → CBMC 有界模型檢查 → 反例分析 → AI 自動修復（政策檢查）→ 重新驗證
  → 程式碼 + JSON / Markdown 驗證報告
```

核心原則：**程式碼是否正確，不由生成它的 AI 判斷**，而由 Clang、Clang Static Analyzer、
Sanitizers 與 CBMC 依照人工批准、雜湊鎖定的規格與 harness 判斷。Lean C 不發明 C 語義，
而是建立在成熟工具之上，並且對結果的範圍誠實：

* `VERIFIED_BOUNDED` 只代表「在已批准的 harness、輸入限制、unwind 界限與檢查類別下沒有找到違反，
  所有迴圈已完全展開，且 harness 非 vacuous」——**永遠不會被升級成 `PROVED`**（V1 沒有無界證明器）。
* 沒有找到反例 ≠ 成功：unwind 不足、vacuous harness、超時、工具錯誤、沒有沙箱而略過的測試，
  都會得到 `UNKNOWN` / `TIMEOUT` / `TOOL_ERROR`。
* 沒有已批准的規格時，唯一可能的結果是 `SPEC_UNAPPROVED`。

---

## 快速開始

### 1. 安裝工具鏈（Linux / WSL2；Ubuntu 24.04 為例）

```bash
sudo apt-get install -y clang-18 libclang-rt-18-dev cbmc bubblewrap
pip install -e ".[dev]"        # Python 3.11+；相依：pydantic、PyYAML
leanc doctor                   # 實際測試 clang、分析器、sanitizers、CBMC、沙箱（含網路隔離）
```

或使用容器：`docker build -t leanc -f docker/Dockerfile .`

### 2. 驗證一個專案（完全離線，不需要 API key）

```bash
leanc init demo                                     # 由範本建立：spec.yaml、標頭、harness、測試、src
leanc spec status demo/spec.yaml                    # => SPEC_UNAPPROVED（exit 40）
leanc verify demo/spec.yaml                         # => SPEC_UNAPPROVED：沒有可信規格就不宣稱任何結果
# 審查 spec.yaml、harness/、tests/ 之後：
leanc spec approve demo/spec.yaml --by alice        # 互動確認；CI 中用 --yes
leanc verify demo/spec.yaml                         # => VERIFIED_BOUNDED（exit 0）
leanc report latest --project-root demo             # Markdown 報告；--format json 取得機器可讀結果
```

### 3. 完整工作流（生成 → 驗證 → 反例 → 修復 → 再驗證）

```bash
cp -r examples/div_by_zero /tmp/dz
leanc spec approve /tmp/dz/spec.yaml --by alice --yes
leanc run /tmp/dz/spec.yaml --provider mock --regenerate
#   initial   COUNTEREXAMPLE_FOUND          (division by zero; INT_MIN / -1 overflow)
#   repair round 1: COUNTEREXAMPLE_FOUND  accepted (improvement; continuing)   <- 只修了一半
#   repair round 2: VERIFIED_BOUNDED  accepted (verified)
#   note: wrote verified sources back to the workspace: src/safe_ratio.c
#   STATUS    VERIFIED_BOUNDED
```

`--provider mock` 使用範例目錄中 `mock/` 的固定輸出（離線、可重現），**它不是 LLM 的表現**，
報告中會明確標示。自然語言需求 → 規格草案也可以離線示範：

```bash
cd demo && leanc spec draft --id my-get --requirement "實作一個安全的整數陣列讀取函式……" --provider mock
#   => drafts/my-get/{spec.yaml,harness_draft.c,tests_draft.c,NOTES.md}，標記為 UNTRUSTED / ai-draft，exit 40
```

使用真實模型：

```bash
export LEANC_API_KEY=...            # 或 OPENAI_API_KEY；永遠不會寫入日誌或報告
export LEANC_MODEL=<model name>     # 任一 OpenAI 相容端點的模型
export LEANC_BASE_URL=https://api.openai.com/v1   # 可改為任何 OpenAI 相容服務
leanc spec draft --id my-func --requirement "實作一個……" --provider openai   # 產生「不受信任」草案
leanc run path/to/spec.yaml --provider openai
```

---

## 指令

| 指令 | 作用 |
|---|---|
| `leanc doctor` | 檢查 Python、Clang、Static Analyzer（SARIF）、Sanitizers（實際在沙箱中執行）、CBMC、bwrap 網路隔離、LLM 設定，並給出修復建議 |
| `leanc init [DIR]` | 由範本建立專案與 `leanc.yaml` |
| `leanc spec draft` | 自然語言需求 → `drafts/<id>/` 下的**不受信任**規格/harness/測試草案 |
| `leanc spec approve` / `status` / `show` | 批准（雜湊鎖定 + HMAC 簽章）、查看批准狀態與變更、顯示正規化規格與有效檢查 |
| `leanc generate` | 依**已批准**規格生成 `sources.editable`（經生成政策檢查） |
| `leanc verify` | spec 檢查 → 編譯 → 完整性關卡（強制）→ 靜態分析 → Sanitizer 測試 → CBMC |
| `leanc repair` | 驗證後執行反例驅動修復迴圈 |
| `leanc run` | 規格檢查 → 生成 → 驗證 → 修復 → 再驗證 → 報告 |
| `leanc report [RUN]` | 顯示某次執行（預設最新）的 Markdown / JSON 報告 |

每個指令都有 `--help` 與明確的結束代碼；除 `report`（用 `--format json`）外都有 `--json`。
`generate` / `verify` / `repair` / `run` 會建立 `.leanc/runs/<run_id>/`，內含 `run.json`、`report.md`、
`leanc.log`；每次驗證的快照、原始工具輸出與工具呼叫日誌在其中的 `verify/`（或 `repair/round-N/verify/`）
子目錄：`snapshot/`、`raw/`（`cbmc-verify.json`、SARIF…）、`logs/`、`verification.json`、`verification.md`。
`doctor`、`init`、`spec approve/status/show` 是即時指令，不建立執行紀錄。
`generate` 成功時 exit 0（但程式碼**尚未驗證**，紀錄中的狀態是 `UNKNOWN`）；`report` 以該次執行的狀態為
exit code，若成功的執行之後原始碼、受保護檔或規格已被修改則顯示 STALE 並回傳 20。
驗證相關指令有 `--timeout-scale`、`--gates`、`--fail-fast`、`--no-sandbox`、`--allow-unsandboxed`、
`--check-unapproved`；LLM 相關有 `--provider`、`--mock-dir`、`--llm-timeout`、`--max-rounds`。

| exit | 狀態 | exit | 狀態 |
|---|---|---|---|
| 0 | `VERIFIED_BOUNDED` / `PROVED` | 20 | `UNKNOWN` |
| 10 | `COMPILE_ERROR` | 21 | `TIMEOUT` |
| 11 | `STATIC_ANALYSIS_WARNING` | 30 | `TOOL_ERROR` |
| 12 | `RUNTIME_TEST_FAILED` | 40 | `SPEC_UNAPPROVED` |
| 13 | `COUNTEREXAMPLE_FOUND` | 50 | `REPAIR_EXHAUSTED` |
| 2 | 使用/輸入錯誤 | 70 | 內部錯誤 |

狀態定義與彙總規則：[docs/status-semantics.md](docs/status-semantics.md)。

---

## 範例與預期結果

| 範例 | 對應測試 | 附帶的程式碼 | `leanc verify` | 偵測工具 | `leanc run --provider mock` |
|---|---|---|---|---|---|
| [`safe_array_get`](examples/safe_array_get) | Test 1 正確程式 | 正確 | `VERIFIED_BOUNDED` | — | `VERIFIED_BOUNDED` |
| [`array_oob`](examples/array_oob) | Test 2 陣列越界 | `index <= count` | `COUNTEREXAMPLE_FOUND`（反例 `count=6, index=6`） | cbmc, asan | 1 回合 → `VERIFIED_BOUNDED` |
| [`div_by_zero`](examples/div_by_zero) | Test 3 除以零 | 未檢查 `den` | `COUNTEREXAMPLE_FOUND`（`den=0`；`INT_MIN/-1`） | cbmc, ubsan | 2 回合（第 1 回合只修一半）→ `VERIFIED_BOUNDED` |
| [`int_overflow`](examples/int_overflow) | Test 4 整數溢位 | 先加再檢查 | `COUNTEREXAMPLE_FOUND`（強制 `signed-overflow`） | cbmc, ubsan | 1 回合 → `VERIFIED_BOUNDED` |
| [`null_deref`](examples/null_deref) | Test 5 指標安全 | 檢查 NULL 但未返回 | `COUNTEREXAMPLE_FOUND` | clang-static-analyzer, cbmc, ubsan | 1 回合 → `VERIFIED_BOUNDED` |
| [`loop_unwind`](examples/loop_unwind) | Test 6 迴圈展開不足 | 正確，但 unwind 5（n ≤ 16 需要 17） | `UNKNOWN`（unwinding assertion 失敗） | cbmc | `UNKNOWN`（不嘗試「修復」）；變體 unwind 17 → `VERIFIED_BOUNDED` |
| [`vacuous_harness`](examples/vacuous_harness) | vacuity | 有 off-by-one，harness assume 互相矛盾 | `UNKNOWN`（純 CBMC 會說 SUCCESSFUL） | cbmc（探針） | `UNKNOWN`；修正 harness 的變體 → `COUNTEREXAMPLE_FOUND` |
| [`runtime_memory`](examples/runtime_memory) | 執行期記憶體 | 錯誤路徑洩漏 | `RUNTIME_TEST_FAILED` | lsan | 1 回合 → `VERIFIED_BOUNDED` |
| [`ring_buffer`](examples/ring_buffer) | 多檔案專案 | 正確（2 個 .c） | `VERIFIED_BOUNDED` | — | `VERIFIED_BOUNDED` |
| [`cheating_repair`](examples/cheating_repair) | 修復作弊 | off-by-one | `COUNTEREXAMPLE_FOUND` | cbmc, asan | 第 1–4 回合被政策拒絕（`__CPROVER_assume`、改 harness、掏空函式、刪 assert + `#pragma`），第 5 回合 → `VERIFIED_BOUNDED` |
| [`repair_exhausted`](examples/repair_exhausted) | 修復耗盡 | `(a+b)/2` 溢位 | `COUNTEREXAMPLE_FOUND` | cbmc, ubsan | `REPAIR_EXHAUSTED`（還原、循環偵測、無提案；工作區不變） |
| [`cbmc_timeout`](examples/cbmc_timeout) | 超時 | 正確，但 64 位元乘法等價性很難 | `TIMEOUT`（3 s） | — | `TIMEOUT` |

規格竄改（修改 harness / 規格 / 批准紀錄 → `SPEC_UNAPPROVED`）、無沙箱（sanitizer `SKIPPED` → `UNKNOWN`）、
`--gates` 子集（→ `UNKNOWN`）等情境由 `tests/` 中的測試涵蓋。

每個範例目錄都有 `expected.yaml`；`tests/test_examples_e2e.py` 與 `scripts/run_experiments.py`
會檢查實際狀態與預期一致。

## 可重現實驗

```bash
python scripts/run_experiments.py        # 結果寫入 experiments/results/results.{json,md}
```

最近一次執行（2026-11-19T14:13:04Z，Ubuntu clang version 18.1.3 (1ubuntu1); cbmc 5.95.1 (cbmc-5.95.1)，Python 3.13.18，`run` 使用 **mock** provider——固定 fixture，**不是** LLM 表現）：

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

**12/12 範例符合預期。** 全部 `verify` 合計 41 s、全部 `run` 合計 65 s（4 核心容器）。
原始資料：[`experiments/results/results.json`](experiments/results/results.json)。

另外，`tests/test_attacks_e2e.py` 收錄了兩輪對抗式審查中發現的 20 種「讓錯誤程式得到 `VERIFIED_BOUNDED`」的攻擊
（覆寫 `__assert_fail` / `nondet_*`、巨集組出的 `_Pragma(CPROVER check disable)`、`#ifdef __clang__`、
`exit`/`abort`/`atexit(_exit)`、linemarker、弱引用 `__VERIFIER_assume`、依輸入大小的 VLA / `alloca`…），現在每一種都會被強制的完整性關卡
擋下，且不會被執行。

詳見 [docs/experiments.md](docs/experiments.md)。

---

## 信任邊界與安全性（摘要）

* **規格與 harness 是可信任的，AI 產物不是。** 批准紀錄綁定規格語意雜湊、harness/測試/標頭的 SHA-256，
  並以存放於工作區外的金鑰做 HMAC 簽章；任何修改都使結果變成 `SPEC_UNAPPROVED`。
* **強制的原始碼完整性關卡**（每次 verify 都執行，不能從規格移除）：以 `llvm-nm`/`llvm-readelf` 檢查編譯後
  的物件檔——只能定義介面標頭宣告的外部符號（不能覆寫 `__assert_fail`、`nondet_*`、`main`、libc 名稱）、
  只能匯入 CBMC 能忠實建模的 libc 函式（禁止 `exit`/`abort`/`atexit`/`fork`/`exec`/弱引用…）、不能有
  constructor/destructor；並在前處理層級拒絕依工具而異的條件編譯（`#ifdef __clang__`、`__OPTIMIZE__`、
  `__CPROVER__`…）、`#pragma`/`_Pragma`、`#line`/linemarker、`__VERIFIER_*`。未通過時不執行任何測試。
* **修復代理只能修改 `sources.editable`**，且不能加入 `__CPROVER_*`/`LEANC_*`/`__builtin_assume`、
  不能刪除 `assert`、不能抑制警告或 sanitizer、不能 `#include` 工作區外檔案、不能新增
  `system`/`exec`/`fork`/`socket` 等呼叫、不能把目標函式改成固定回傳值；重複補丁被偵測為循環；
  讓結果變成 `UNKNOWN`/`TIMEOUT` 不算改善。
* **不可信程式只在 bubblewrap 沙箱中執行**（無網路、丟棄 capabilities、uid nobody、唯讀系統目錄、
  容量受限的私有 `/tmp`、CPU/記憶體/時間/輸出/檔案大小限制）；沒有可用沙箱時拒絕執行並回報 `UNKNOWN`，
  不會假裝已隔離。只有 CLI 旗標 `--allow-unsandboxed` 能放寬（專案設定檔不能）。
* **批准在驗證過程中被重新檢查**（CBMC 之前與所有關卡之後），且以工作區外的簽章帳本防止回滾到舊版本。
* 不使用 `shell=True`；子行程使用最小環境；日誌、報告與 LLM transcript 都經過憑證遮蔽。

詳見 [docs/trust-model.md](docs/trust-model.md) 與 [docs/security.md](docs/security.md)。

## 專案結構

```
leanc/
  cli.py                 CLI（argparse）
  core/                  狀態、資料模型、控制器、安全執行、沙箱、工作區、設定、doctor、生成
  specs/                 規格 schema、載入/雜湊、批准（信任邊界）
  llm/                   Provider 抽象、OpenAI 相容 provider、Mock provider、prompts
  backends/              clang、analyzer（SARIF）、sanitizer、cbmc、工具偵測
  repair/                反例解析、補丁、修復政策、修復迴圈
  reports/               JSON 與 Markdown 報告
  data/include/leanc.h   harness 巨集（LEANC_ASSUME / LEANC_REACH / nondet_*）
  data/templates/        `leanc init` 範本
examples/                可驗證的範例專案（含 mock 固定輸出與 expected.yaml）
tests/                   pytest：單元測試 + 實際工具整合測試 + 範例端到端測試
scripts/run_experiments.py
docs/                    架構、規格格式、信任模型、狀態語意、安全性、實驗、roadmap
docker/Dockerfile        可重現環境
```

## 限制

* `VERIFIED_BOUNDED` 是有界結果；harness 是否忠實表達需求，仍需人工審查。
* Sanitizer 只涵蓋可信測試實際執行到的路徑。
* 修復政策是縱深防禦（文字規則），真正的防線是受保護的 harness 與獨立驗證器。
* V2（C++20、Frama-C/ACSL、Lean 4）尚未實作，見 [docs/roadmap.md](docs/roadmap.md)。

## 授權

Apache-2.0，見 [LICENSE](LICENSE)。
