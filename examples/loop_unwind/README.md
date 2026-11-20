# loop_unwind — 迴圈展開不足 (Test 6)

需求：「實作一個函式，計算整數陣列中所有元素的總和，並以 long long 回傳，不得發生溢位；陣列為 NULL 或長度為 0 時回傳 0。」

程式碼本身是**正確的**。問題出在規格：harness 探索 `n <= 16`，但 `spec.yaml` 的
`verification.unwind` 只有 5。CBMC 的 unwinding assertion `sum_array.unwind.0` 因此失敗 ——
「沒有找到反例」只涵蓋了 `n <= 4` 的執行，對 `n = 5..16` 什麼也沒證明。

| 檔案 | 角色 | 信任 |
|---|---|---|
| `spec.yaml` | 規格（`unwind: 5`，故意不足；harness 自己的迴圈以 `unwindset` 給足 17） | 批准後鎖定 |
| `spec_full_unwind.yaml` | 修正後的規格變體（`spec_id: loop-unwind-full`，`unwind: 17`），需另行批准 | 批准後鎖定 |
| `include/sum_array.h` | 介面 | 受保護 |
| `harness/sum_array_harness.c` | CBMC harness：任意 `int` 值、`n <= 16`、64 位元參考總和、NULL 輸入、frame 檢查、`LEANC_REACH` 探針 | 受保護 |
| `tests/test_sum_array.c` | 可信任測試（含 16 個極值元素，確認不會 int 溢位） | 受保護 |
| `src/sum_array.c` | 實作（正確） | 可編輯 |
| `mock/generate/` | Mock LLM 固定輸出（與 `src/` 相同；非真實 LLM 表現） | — |

```bash
leanc spec approve examples/loop_unwind/spec.yaml --by alice --yes
leanc verify examples/loop_unwind/spec.yaml              # => UNKNOWN (exit 20): unwinding assertion failed
leanc run examples/loop_unwind/spec.yaml --provider mock # => UNKNOWN: 不是程式碼缺陷，不進入修復迴圈

leanc spec approve examples/loop_unwind/spec_full_unwind.yaml --by alice --yes
leanc verify examples/loop_unwind/spec_full_unwind.yaml  # => VERIFIED_BOUNDED (exit 0)
```

預期結果（`expected.yaml`）：

| 指令 | 狀態 | 原因 |
|---|---|---|
| `verify spec.yaml` | `UNKNOWN` | compile / analyzer / sanitizer 皆通過；CBMC 唯一的失敗是 `sum_array.unwind.0`（unwinding assertion loop 0, `src/sum_array.c:10`） |
| `run --provider mock` | `UNKNOWN` | `UNKNOWN` 不可修復：改程式碼無法彌補不足的界限；修復代理不會被呼叫（0 輪） |
| `verify spec_full_unwind.yaml` | `VERIFIED_BOUNDED` | `unwind: 17` 足夠，所有探針（after_call, empty, partial, max_len, null_input）可達 |

重點：提高界限是**人類的決定**，必須以新的規格（不同 `spec_id` 或提高 `version`）重新批准；
AI 修復代理不能也不會修改規格。注意 `max_len` 探針在 `unwind: 5` 下是不可達的 ——
這正是「界限不足時驗證什麼都不代表」的直接證據。

CBMC 成本（本機 CBMC 5.95.1）：`spec.yaml` 約 0.2 s；`spec_full_unwind.yaml` 主驗證約 5–8 s、vacuity 約 5 s
（16 個 64 位元加法鏈的等價性證明）。
