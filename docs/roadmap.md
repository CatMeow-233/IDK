# Roadmap 與 V1 範圍

## V1——已實作並測試

* C17 單檔案與小型多檔案專案（`sources.editable` 多個 `.c`，例如 `examples/ring_buffer`）
* 自然語言需求 → **不受信任**的規格草案（`leanc spec draft`）→ 人工批准（雜湊鎖定 + HMAC 簽章）
* AI 程式碼生成（`leanc generate`）、OpenAI 相容 provider 與離線 Mock provider
* Clang 編譯（診斷解析、warning 政策）、Clang Static Analyzer（SARIF，含 code flow）
* ASan / UBSan / LSan 可信測試，在 bubblewrap 沙箱中執行（無網路、資源限制）
* CBMC 有界模型檢查（強制檢查類別、unwinding assertions、vacuity 探針、可選覆蓋率）
* 反例解析（性質、位置、輸入、呼叫堆疊、trace）與 Failure Report
* 反例驅動修復迴圈（政策檢查、循環偵測、改善判定/還原、最多 N 回合、REPAIR_EXHAUSTED）
* JSON / Markdown 報告、CLI（doctor / init / spec / generate / verify / repair / run / report）
* 自動化測試與可重現實驗（`scripts/run_experiments.py`）

## V2（已實作）

| 項目 | 實作 | 狀態影響 |
|---|---|---|
| Frama-C/WP + ACSL | `leanc/backends/wp/`、`leanc/rules/acsl.py`、`leanc/toolchain/framac.py`；契約在受保護標頭；`-wp-rte`、smoke test、停用快取；證明修復（AI 加 loop invariant） | 唯一能產生 `PROVED` 的途徑 |
| Lean 4（可選） | `leanc/backends/lean/`、`leanc/rules/lean_rules.py`、`leanc/toolchain/lean.py`；受保護模型與定理敘述、AI 撰寫證明、kernel + `leanchecker`、只接受標準公理、沙箱執行、差分測試向量 | 模型層級；未證明 → `UNKNOWN`；向量不符 → `RUNTIME_TEST_FAILED`；不會產生 `PROVED` |
| C++20 | `leanc/toolchain/cxx.py`、`leanc/rules/cpp_rules/`、`leanc/lang/cpp_lexer.py`；clang++ 編譯 / 分析 / sanitizer；C++ 專屬完整性規則（禁止例外、RTTI、coroutine、全域建構子、取代 `operator new`、寫入 `std` …） | 沒有可用的 C++ model checker → 最佳 `UNKNOWN` |

## Inductive profile（opt-in，已實作）

| 項目 | 實作 | 狀態影響 |
|---|---|---|
| CBMC DFCC 迴圈契約 | `leanc/backends/inductive/`、`leanc/rules/loop_contracts.py`、`leanc/rules/loop_sites.py`、`leanc/lang/c_expr.py`、`leanc/toolchain/cprover.py`、`leanc/data/runtime/inductive_models.c`；受保護的 inductive harness、可編輯的契約檔（白名單子句文法）、先可信後可編輯的連結與符號表檢查、殘留迴圈檢查、函式 / 迴圈 / 返回探針、只用強制檢查、CBMC 5.95.1 版本鎖定；修復只能改契約檔（`CONTRACTS_ONLY_ROUND`、`INDUCTIVE_CODE_EDIT`）；原因代碼（`reason_codes`） | `VERIFIED_INDUCTIVE`（exit 0），需要 `cbmc` 也通過；不會產生 `PROVED`；未通過時最好是 `UNKNOWN` |

## 之後（V3 方向，未實作）

| 項目 | 方向 |
|---|---|
| C++ 形式化關卡 | ESBMC（clang 前端，支援現代 C++）。在本環境中執行下載的 ESBMC 執行檔未獲許可，因此尚未整合 |
| 多 TU 靜態分析 | Clang CTU |
| Harness / 契約審查輔助 | 變異測試、ACSL 契約強度評估（harness 的位置覆蓋率已由 `verification.coverage` 提供） |
| 更強的隔離 | gVisor / Firecracker / 每次執行一個容器 |
| inductive profile 的後續 | 只標記不阻擋的審查子代理、原生反例重播 |

## 已知限制

* `VERIFIED_BOUNDED` 只涵蓋 harness 所描述的輸入空間與界限，不是無界證明；`PROVED` 只涵蓋已批准的 ACSL 契約；`VERIFIED_INDUCTIVE`
  只涵蓋受保護 inductive harness 照原樣撰寫的執行、強制檢查類別與 harness 斷言（harness 的其他程式碼也會縮小涵蓋的狀態）。
* 規格品質取決於人工審查；Lean C 能檢查「程式是否符合 harness」，不能檢查「harness 是否符合需求」。
* 修復政策是文字層級的縱深防禦，不是完整的語意分析。
* CBMC 對大型迴圈、複雜指標結構、浮點數可能很慢；以逾時回報 `TIMEOUT`，不會假裝成功。
