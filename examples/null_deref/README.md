# null_deref — 檢查了 NULL 卻仍然解參考 (Test 5)

需求：「實作 sensor_read(s, out)……s 為 NULL 時必須回傳錯誤、把 *out 設為 0，而且絕不能解參考 s……」

**缺陷**（`src/sensor.c`，即 mock「生成」的程式）：`s == NULL` 的分支只記下錯誤碼
（`rc = SENSOR_ERR_NULL;`）卻沒有 `return`，接著執行 `if (!s->enabled)`——
正是在偵測到 NULL 的那條路徑上解參考了 NULL。

| 檔案 | 角色 | 信任 |
|---|---|---|
| `spec.yaml` | 結構化規格（前置/後置條件、安全性質、界限、關卡） | 批准後鎖定 |
| `include/sensor.h` | 介面（`struct sensor`、回傳碼） | 受保護 |
| `harness/sensor_read_harness.c` | CBMC harness：`struct sensor` 各欄位任意、`s` 與 `out` 各自可為 NULL；檢查回傳碼、`*out` 的值與結構未被修改 | 受保護 |
| `tests/test_sensor_read.c` | 可信任測試（含 `null_sensor` 案例） | 受保護 |
| `src/sensor.c` | 出貨的有缺陷實作 | 可編輯 |
| `mock/generate/src/sensor.c` | Mock LLM「生成」的程式（與出貨版相同） | — |
| `mock/repair/round-1.json` | 正確修復：`s == NULL` 時立即 `return SENSOR_ERR_NULL` | — |

```bash
leanc spec approve examples/null_deref/spec.yaml --by alice --yes
leanc verify examples/null_deref/spec.yaml                           # => COUNTEREXAMPLE_FOUND (exit 13)
leanc run examples/null_deref/spec.yaml --provider mock --regenerate   # => VERIFIED_BOUNDED（1 輪修復）
```

## 預期狀態（`expected.yaml`）

| | 狀態 | 說明 |
|---|---|---|
| `verify`（出貨程式） | `COUNTEREXAMPLE_FOUND` | 三個關卡都失敗：static_analysis（`STATIC_ANALYSIS_WARNING`，policy=block）、sanitizer（`RUNTIME_TEST_FAILED`）、cbmc；整體狀態取最嚴重者 |
| `run --provider mock` | `VERIFIED_BOUNDED` | round 1 後所有關卡通過 |

## 由哪個工具偵測到什麼（實際觀察到的輸出）

| 工具 | 結果 |
|---|---|
| clang（編譯） | 無警告 |
| **Clang Static Analyzer** | `core.NullDereference`：`Access to field 'enabled' results in a dereference of a null pointer (loaded from variable 's')`（`src/sensor.c:14`） |
| **UBSan** | `null_sensor`：`src/sensor.c:14:13: runtime error: member access within null pointer of type 'const struct sensor'` |
| **CBMC** | `sensor_read.pointer_dereference.7`：`dereference failure: pointer NULL in s->enabled`（第 14 行），反例 `s = NULL`、`out = &out_cell`；同一 NULL 指標也觸發 `s->value`（第 17 行）與其他指標子檢查，以及 harness 斷言 `rc == SENSOR_ERR_NULL`、`out_cell == 0`（12 of 34 failed） |

修復後：CBMC `VERIFICATION SUCCESSFUL`，5 個探針（`after_call`、`null_out`、`null_sensor`、
`disabled`、`enabled`）皆可達；4 個測試全部通過；分析器無發現。
