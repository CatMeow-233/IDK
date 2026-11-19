# int_overflow — 先相加再檢查溢位（太遲了）(Test 4)

需求：「實作一個檢查溢位的整數加法函式 checked_add(a, b, out)……溢位必須在相加『之前』判斷，
因為有號整數溢位在 C 中是未定義行為。」

**缺陷**（`src/checked_add.c`，即 mock「生成」的程式）：

```c
sum = a + b;                                       /* 溢位（UB）在這裡已經發生 */
if ((b > 0 && sum < a) || (b < 0 && sum > a)) {   /* 事後檢查；最佳化器可以把它刪掉 */
```

在 CBMC 的「環繞」模型中事後檢查看起來是對的——harness 的所有後置條件都成立——
**只有**強制開啟的 `--signed-overflow-check` 抓得到這個缺陷。

| 檔案 | 角色 | 信任 |
|---|---|---|
| `spec.yaml` | 結構化規格（前置/後置條件、安全性質、界限、關卡） | 批准後鎖定 |
| `include/checked_add.h` | 介面（回傳碼） | 受保護 |
| `harness/checked_add_harness.c` | CBMC harness：`a`、`b` 完全不受限，`out` 可為 NULL；以 64 位元計算精確和，檢查回傳碼與 `*out` 的精確值 | 受保護 |
| `tests/test_checked_add.c` | 可信任測試（含 INT_MAX + 1、INT_MIN + -1 與各邊界） | 受保護 |
| `src/checked_add.c` | 出貨的有缺陷實作 | 可編輯 |
| `mock/generate/src/checked_add.c` | Mock LLM「生成」的程式（與出貨版相同） | — |
| `mock/repair/round-1.json` | 正確修復：相加前檢查 `a > INT_MAX - b` / `a < INT_MIN - b`（不需 `__builtin_*`） | — |

```bash
leanc spec approve examples/int_overflow/spec.yaml --by alice --yes
leanc verify examples/int_overflow/spec.yaml                           # => COUNTEREXAMPLE_FOUND (exit 13)
leanc run examples/int_overflow/spec.yaml --provider mock --regenerate   # => VERIFIED_BOUNDED（1 輪修復）
```

## 預期狀態（`expected.yaml`）

| | 狀態 | 說明 |
|---|---|---|
| `verify`（出貨程式） | `COUNTEREXAMPLE_FOUND` | sanitizer 關卡也失敗（2 個測試） |
| `run --provider mock` | `VERIFIED_BOUNDED` | round 1 後所有關卡通過 |

## 由哪個工具偵測到什麼（實際觀察到的輸出）

| 工具 | 結果 |
|---|---|
| clang（編譯） | 無警告 |
| Clang Static Analyzer | 無發現 |
| **UBSan** | `positive_overflow`：`src/checked_add.c:10:13: runtime error: signed integer overflow: 2147483647 + 1 cannot be represented in type 'int'`；`negative_overflow`：`-2147483648 + -1 cannot be represented` |
| **CBMC** | 唯一的失敗：`checked_add.overflow.1`：`arithmetic overflow on signed + in a + b`（第 10 行），反例例如 `a = -2147439493, b = -58607`（1 of 16 failed） |

修復後：CBMC `VERIFICATION SUCCESSFUL`（完整 int 範圍），5 個探針（`after_call`、`null_out`、
`positive_overflow`、`negative_overflow`、`representable`）皆可達；5 個測試全部通過；分析器無發現。
