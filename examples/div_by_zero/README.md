# div_by_zero — 除以零，修復後出現新的反例 (Test 3)

需求：「實作一個安全的整數除法函式 safe_ratio(num, den, out)……除數為 0 時必須回傳錯誤；
結果無法以 int 表示時（INT_MIN / -1）也必須回傳錯誤……任何情況下都不得發生未定義行為。」

**缺陷**（`src/safe_ratio.c`，即 mock「生成」的程式）：直接計算 `*out = num / den;`，
既沒有檢查 `den == 0`，也沒有檢查唯一會溢位的組合 `INT_MIN / -1`。CBMC 對同一個除法同時檢查
`--div-by-zero-check` 與 `--signed-overflow-check`，因此兩個失敗同時出現。

| 檔案 | 角色 | 信任 |
|---|---|---|
| `spec.yaml` | 結構化規格（前置/後置條件、安全性質、界限、關卡） | 批准後鎖定 |
| `include/safe_ratio.h` | 介面（回傳碼、檢查順序） | 受保護 |
| `harness/safe_ratio_harness.c` | CBMC harness：`num`、`den` **完全不受限**（全部 int），`out` 可為 NULL；每個分支一個探針 | 受保護 |
| `tests/test_safe_ratio.c` | 可信任測試（含 `zero_den`、`int_min_by_minus_one`、INT_MIN/INT_MAX 邊界） | 受保護 |
| `src/safe_ratio.c` | 出貨的有缺陷實作 | 可編輯 |
| `mock/generate/src/safe_ratio.c` | Mock LLM「生成」的程式（與出貨版相同） | — |
| `mock/repair/round-1.json` | **不完整**修復：只處理 `den == 0`（刻意保留除法所在行不變） | — |
| `mock/repair/round-2.json` | 完整修復：再加上 `num == INT_MIN && den == -1` | — |

```bash
leanc spec approve examples/div_by_zero/spec.yaml --by alice --yes
leanc verify examples/div_by_zero/spec.yaml                          # => COUNTEREXAMPLE_FOUND (exit 13)
leanc run examples/div_by_zero/spec.yaml --provider mock --regenerate  # => VERIFIED_BOUNDED（2 輪修復）
```

## 預期狀態（`expected.yaml`）

| | 狀態 | 說明 |
|---|---|---|
| `verify`（出貨程式） | `COUNTEREXAMPLE_FOUND` | sanitizer 關卡也失敗（2 個測試） |
| round 1 後 | `COUNTEREXAMPLE_FOUND` | 除以零的失敗消失，溢位失敗（同一性質、同一行）仍在、沒有新增失敗 → 嚴格改善，**接受** |
| `run --provider mock` | `VERIFIED_BOUNDED` | round 2 後所有關卡通過 |

## 由哪個工具偵測到什麼（實際觀察到的輸出）

| 程式版本 | 工具 | 結果 |
|---|---|---|
| 出貨 | **CBMC** | `safe_ratio.division-by-zero.1`：`division by zero in num / den`（第 8 行），反例 `num = 4, den = 0`；`safe_ratio.overflow.1`：`arithmetic overflow on signed division in num / den`（第 8 行），反例 `num = -2147483648, den = -1`；harness 斷言 `rc == SAFE_RATIO_ERR_DIV_ZERO`、`rc == SAFE_RATIO_ERR_OVERFLOW`、`*out` 未被修改 共 4 個失敗（6 of 25 failed） |
| 出貨 | **UBSan** | `zero_den`：`src/safe_ratio.c:8:16: runtime error: division by zero`；`int_min_by_minus_one`：`division of -2147483648 by -1 cannot be represented in type 'int'` |
| 出貨 | Clang Static Analyzer | 無發現（沒有任何路徑上 `den` 已知為 0） |
| round 1 | CBMC / UBSan | 只剩 `safe_ratio.overflow.1`（第 8 行，`num = INT_MIN, den = -1`）與對應的 2 個 harness 斷言（3 of 25 failed）；UBSan 只剩 `int_min_by_minus_one` |
| round 2 | 全部 | CBMC `VERIFICATION SUCCESSFUL`，7 個探針皆可達；5 個測試全部通過；分析器無發現 |

## 驗證範圍

除以零、溢位與所有回傳碼/`*out` 未修改的後置條件都在**完整 int 範圍**上檢查。商的數值：
在完整範圍上以符號與大小刻畫截斷除法（`|q| <= |num|`、`q == 0` 當且僅當 `|num| < |den|`、
非零的 `q` 為負當且僅當 `num`、`den` 異號），並對 `den == ±1`（任意 `num`）以及
`|num| <= 256, |den| <= 16` 的每一組輸入檢查精確值 `q == num / den`。
在全部 2^64 組輸入上證明兩個 32 位元除法器等價，對 CBMC 的 SAT 後端不可行（> 100 秒）；
INT_MIN/INT_MAX 等極值的精確商由可信任測試覆蓋。
