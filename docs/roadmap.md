# Roadmap 與 V1 範圍

## V1（本版本）——已實作並測試

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

## V2（規劃，未實作）

V1 刻意**沒有**留下這些功能的空殼模組；以下是設計方向：

| 項目 | 方向 | 對狀態的影響 |
|---|---|---|
| C++20 | `language.name: cpp`；clang++ 後端；CBMC 的 C++ 前端支援有限，可能需要 ESBMC 或僅限 C 子集 | 新的 gate 實作，沿用 `StageResult` |
| Frama-C / ACSL | 由已批准規格產生（或人工撰寫）ACSL 契約；`frama-c -wp` 後端；每個 proof obligation 的結果記錄進報告 | 所有 PO 皆被證明且無 `admit` 時，才可能是 **`PROVED`**（在 ACSL 契約與 WP 記憶體模型的前提下） |
| Lean 4 | 對純函式的數學規格做證明（例如 `Nat` 上的演算法性質），C 實作透過 refinement 或測試對應 | 只在有機器檢查的證明物件時給 `PROVED` |
| 多 TU 靜態分析 | Clang CTU（`-analyzer-config experimental-enable-naive-ctu-analysis`） | 減少單 TU 分析的誤判/漏判 |
| Harness 審查輔助 | 以覆蓋率（`--cover`）與變異測試（mutation）評估 harness 強度，提示過強的 assume / 過弱的 assert | 只提供建議，不自動修改可信檔案 |
| 更強的隔離 | 以 gVisor / Firecracker / 容器 per-run 執行 | 沙箱種類記錄於報告 |

## 已知限制（V1）

* `VERIFIED_BOUNDED` 只涵蓋 harness 所描述的輸入空間與界限，不是無界證明。
* 規格品質取決於人工審查；Lean C 能檢查「程式是否符合 harness」，不能檢查「harness 是否符合需求」。
* 修復政策是文字層級的縱深防禦，不是完整的語意分析。
* CBMC 對大型迴圈、複雜指標結構、浮點數可能很慢；以逾時回報 `TIMEOUT`，不會假裝成功。
