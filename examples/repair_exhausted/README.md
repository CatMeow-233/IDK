# repair_exhausted — 修復輪數用盡

需求：「實作一個函式，回傳兩個 int 的中點 (a + b) / 2（向零取整），對所有 int 輸入都必須正確，且計算過程中不得發生有號整數溢位。」

已出貨的 `src/midpoint.c` 以 `int` 計算 `a + b`，同號大數時發生有號溢位（未定義行為）。

| 工具 | 發現 |
|---|---|
| CBMC | `midpoint.overflow.1`：`arithmetic overflow on signed + in a + b`（`src/midpoint.c:5`，`a=-1103143402, b=-1103118345`）；harness 的 POST1 `(long long)m == exact`、POST2 `lo <= m && m <= hi` |
| UBSan | `extremes` 測試：`signed integer overflow: 2147483647 + 2147483647 cannot be represented in type 'int'` |
| Clang Static Analyzer | 無 |

Mock 修復提案（固定示範資料，非真實 LLM 表現）—— 沒有一輪是正確修正：

| 輪 | 提案 | 修復迴圈的處理 |
|---|---|---|
| 1 | 只把 `sum` 改名為 `total`（外觀修改） | 通過政策 → 重新驗證仍為 `COUNTEREXAMPLE_FOUND` → 不是改進，**還原** |
| 2 | 與第 1 輪逐位元組相同 | **循環偵測**（candidate hash 已出現過），不重新驗證 |
| 3 | `a / 2 + b / 2`（不會溢位，但捨入錯誤：`midpoint(1, 1) == 0`） | 通過政策 → `COUNTEREXAMPLE_FOUND`（POST1、POST2 失敗）＋測試失敗 → 引入新失敗，**還原** |
| 4 | （無 fixture） | 「沒有提案」，記錄為失敗的一輪 |
| 5 | （無 fixture） | 同上 |

`repair.max_rounds: 5` 用盡 → 最終狀態 `REPAIR_EXHAUSTED`（exit 50）；工作區保持原樣，最佳候選存放在
`run_dir/best-candidate/`。（真正的修正，例如 `(int)(((long long)a + b) / 2)`，以原始工具實測可通過全部 gate ——
harness 本身是可滿足的，失敗的是修復代理。）

| 檔案 | 角色 | 信任 |
|---|---|---|
| `spec.yaml` | 規格（無前置條件：所有 `int` 組合） | 批准後鎖定 |
| `include/midpoint.h` | 介面 | 受保護 |
| `harness/midpoint_harness.c` | CBMC harness：64 位元參考值、範圍後置條件、探針 `same_sign` / `opposite_sign` / `odd_sum` | 受保護 |
| `tests/test_midpoint.c` | 可信任測試（含 `INT_MAX`/`INT_MIN` 極值與奇數和的捨入） | 受保護 |
| `src/midpoint.c` | 實作（會溢位） | 可編輯 |
| `mock/generate/`, `mock/repair/round-{1,2,3}.json` | Mock 生成 / 修復提案 | — |

```bash
leanc spec approve examples/repair_exhausted/spec.yaml --by alice --yes
leanc verify examples/repair_exhausted/spec.yaml               # => COUNTEREXAMPLE_FOUND (exit 13)
leanc run examples/repair_exhausted/spec.yaml --provider mock  # => REPAIR_EXHAUSTED (exit 50)
```
