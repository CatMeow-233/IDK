# 信任模型（Trust Model）

> 程式碼是否正確，不能只由生成程式碼的 AI 自己判斷。

Lean C 把所有產物分成三類：

| 類別 | 例子 | 誰可以寫 | 如何保護 |
|---|---|---|---|
| **可信任**（trusted） | 已批准的 `spec.yaml`、CBMC harness、可信測試 driver、介面標頭 | 只有人類（審查後 `leanc spec approve`） | 語意雜湊 + 檔案 SHA-256 + HMAC 簽章；每次驗證前在快照上重新檢查 |
| **不受信任的候選**（untrusted） | AI 規格草案、AI harness 草案、AI 生成/修復的 C 原始碼 | LLM provider（經政策檢查後） | 只能寫入 `sources.editable`；內容政策；沙箱執行 |
| **獨立裁判**（judges） | Clang、Clang Static Analyzer、ASan/UBSan/LSan、CBMC | — | Lean C 只解析其輸出，不採信 LLM 的任何「已正確」說法 |

## 批准紀錄

`leanc spec approve <spec> --by <name>` 會：

1. 以 pydantic 驗證規格（未知欄位、YAML 重複鍵、路徑穿越、editable/受保護重疊 → 拒絕）；
2. 計算規格的**語意雜湊**（正規化後的 JSON；註解/排版變更不影響，任何欄位變更都會改變）；
3. 計算每個受保護檔（harness、測試 driver、標頭）的 SHA-256；
4. 寫入 `.leanc/approvals/<spec_id>.json`（`current` + `history`），並以存放在**工作區外**的金鑰
   （`$LEANC_HOME/approval.key`，預設 `~/.config/leanc/approval.key`，權限 0600）計算 HMAC-SHA256 簽章。

版本規則：內容與目前已批准內容不同時，`version` 必須嚴格遞增；相同內容可重複批准（冪等）。
`leanc spec status` 會顯示自上次批准以來的欄位層級差異。

## 驗證前檢查（每次 verify）

`check_approval` 在 **快照** 上檢查，任何一項失敗即 `SPEC_UNAPPROVED`：

* 有批准紀錄、`spec_id` 與 `version` 相符；
* 規格語意雜湊相符（規格被改 → 失效）；
* 受保護檔集合相同且每個檔案雜湊相符（harness/測試/標頭被改 → 失效）；
* 簽章金鑰存在、`key_id` 相符、HMAC 驗證通過（偽造或竄改紀錄 → 失效）。

## 為什麼修復代理無法「作弊」

| 作弊手法 | 防線 |
|---|---|
| 修改 harness / 測試 / 標頭 / 規格 / 批准紀錄 | 修復政策 `PATH_NOT_EDITABLE`（只允許 `sources.editable`）；即使繞過，雜湊與簽章檢查也會使結果變成 `SPEC_UNAPPROVED` |
| 刪除失敗的 assert | harness 受保護；原始碼中 `assert(` 數量減少 → `ASSERT_REMOVED` |
| 在原始碼加 `__CPROVER_assume` / `LEANC_ASSUME` / `__builtin_assume` / `__builtin_unreachable` | `CPROVER_BUILTIN` / `ASSUMPTION_INJECTION` |
| 降低驗證強度（關閉 `--signed-overflow-check`、縮小 unwind、縮小輸入範圍） | 檢查類別、unwind、輸入限制都在已批准規格/harness 中；`MANDATORY_CHECKS` 不能被規格關閉；修復代理只能改原始碼 |
| 讓 harness 變成 vacuous（例如讓函式 `exit(0)` / `abort()` 或無窮迴圈） | 完整性關卡拒絕 `exit`/`_exit`/`abort`/`atexit`… 匯入（CBMC 把它們建模為 `assume(false)`，會悄悄刪掉輸入）；探針以 CBMC property 為單位計算，任何一個不可達 → `UNKNOWN`；無窮迴圈 → unwinding assertion 失敗 → `UNKNOWN`；都不算改善 |
| 覆寫 harness 依賴的符號（`__assert_fail` 讓所有 assert 消失、`nondet_*` 縮小輸入空間、`main`、libc 函式） | 完整性關卡：物件檔的外部符號必須是介面標頭宣告的名稱；保留名稱、`nondet_*`、弱符號一律拒絕 |
| 讓不同工具看到不同程式（`#ifdef __clang__`、`__OPTIMIZE__`、`__CPROVER__`、`LEANC_VACUITY_CHECK`、`NDEBUG`） | 完整性關卡拒絕在前處理指令中使用保留識別字/工具巨集 |
| 以巨集組出 `_Pragma(CPROVER check disable ...)` 關閉檢查、`#line`/linemarker 偽裝成系統標頭以隱藏分析器警告、`__VERIFIER_assume` | 完整性關卡（前處理後掃描 + 指令規則） |
| 在建構子中 `atexit(_exit(0))` 偽造測試結果 / 躲過 LSan | 拒絕 `.init_array`/`.ctors` 與 `atexit`/`_exit` 匯入；未通過則不執行測試 |
| 測試程式在執行中竄改快照中的 harness | 沙箱只給測試程式自己的目錄（唯讀）與空白案例目錄；批准在 CBMC 前與結束時於快照上再檢查 |
| 把批准紀錄回滾到被取代的舊版本 | `$LEANC_HOME` 中的簽章帳本記錄每個專案+規格已批准的最高版本 |
| 吞掉錯誤 / 固定回傳值 | harness 的後置條件 assert（例如 `*out == arr[idx]`）+ `TRIVIALIZED_TARGET` 啟發式 |
| 關閉警告 / sanitizer | `WARNING_SUPPRESSION`（`#pragma clang diagnostic`）、`SANITIZER_SUPPRESSION`（`no_sanitize`） |
| 嘗試存取主機 / 網路 | `DANGEROUS_CALL`（system/popen/exec/fork/socket/...）+ 沙箱（無網路、唯讀系統目錄、uid nobody） |
| `#include "/etc/shadow"` 洩漏資料 | `INCLUDE_ESCAPE`；編譯器/CBMC 也在沙箱中執行，只看得到 `/usr` 與快照 |
| 反覆提交同一個補丁 | 候選雜湊去重（cycle detection），不重新驗證、計入回合 |
| 讓結果變成 UNKNOWN/TIMEOUT 以「消除」反例 | `is_improvement` 規則：不確定結果永遠不算改善，會被還原 |

## AI 生成的規格與 harness

`leanc spec draft` 產出的檔案一律寫在 `drafts/<id>/`，並標記 `provenance.origin: ai-draft` 與
「UNTRUSTED」檔頭；它們在人工審查並批准之前**不會**被任何驗證採用。批准 AI 草案時，
互動式確認會明確警告。系統不會自動批准任何東西；`scripts/run_experiments.py` 與測試會用
暫時的 `LEANC_HOME` 扮演審查者，批准的是人工撰寫的範例。

## 規格 → 證明義務

YAML 中的自然語言前置/後置條件本身**不會**被 CBMC 證明。轉換機制是：

* 前置條件 → harness 中的 `LEANC_ASSUME(...)`（CBMC `__CPROVER_assume`）；
* 後置條件、frame 條件、回傳狀態 → harness 中的 `assert(...)`；
* 安全性質 → CBMC 檢查類別（`bounds`、`pointer`、`div-by-zero`、`signed-overflow`…）+ sanitizer；
* 輸入範圍 → harness 中的 assume（`verification.input_bounds` 只是給人看的紀錄，會寫進報告的範圍說明）。

因此審查 harness 是批准流程中最重要的一步：它就是形式化規格。
