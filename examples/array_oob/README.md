# array_oob — 差一錯誤造成越界讀取 (Test 2)

需求：「實作一個整數陣列查詢函式 array_lookup……索引越界時必須回傳錯誤，絕不能讀取陣列範圍以外的記憶體。」

**缺陷**（`src/array_lookup.c`，即 mock「生成」的程式）：範圍檢查寫成 `index <= count`，
於是 `index == count` 被當成合法索引，讀取了陣列結尾之後的 `values[count]`。

| 檔案 | 角色 | 信任 |
|---|---|---|
| `spec.yaml` | 結構化規格（前置/後置條件、安全性質、界限、關卡） | 批准後鎖定 |
| `include/array_lookup.h` | 介面（回傳碼） | 受保護 |
| `harness/array_lookup_harness.c` | CBMC harness：`count <= 8`、`index` 任意、`values`/`out` 可為 NULL；陣列物件**恰好** `count` 個元素（`malloc(count * sizeof(int))`），因此任何 `values[count]` 都在物件之外 | 受保護 |
| `tests/test_array_lookup.c` | 可信任測試；陣列放在 heap 上且大小精確，`index_equals_count` 案例讓 ASan 看見越界 | 受保護 |
| `src/array_lookup.c` | 出貨的有缺陷實作（AI 可修復） | 可編輯 |
| `mock/generate/src/array_lookup.c` | Mock LLM「生成」的程式（與出貨版相同） | — |
| `mock/repair/round-1.json` | Mock 修復提案：`index < count`（正確修復） | — |

```bash
leanc spec approve examples/array_oob/spec.yaml --by alice --yes
leanc verify examples/array_oob/spec.yaml                       # => COUNTEREXAMPLE_FOUND (exit 13)
leanc run examples/array_oob/spec.yaml --provider mock --regenerate   # => VERIFIED_BOUNDED（1 輪修復）
```

## 預期狀態（`expected.yaml`）

| | 狀態 | 說明 |
|---|---|---|
| `verify`（出貨程式） | `COUNTEREXAMPLE_FOUND` | sanitizer 關卡也失敗（`RUNTIME_TEST_FAILED`），但反例優先順序較高 |
| `run --provider mock` | `VERIFIED_BOUNDED` | round 1 修正為 `index < count` 後所有關卡通過 |

## 由哪個工具偵測到什麼（實際觀察到的輸出）

| 工具 | 結果 |
|---|---|
| clang（編譯） | 無警告 |
| Clang Static Analyzer | 無發現（分析器不知道陣列大小） |
| **ASan** | `index_equals_count`：`AddressSanitizer: heap-buffer-overflow ... src/array_lookup.c:9:16 in array_lookup` |
| **CBMC** | `array_lookup.pointer_dereference.11`：`dereference failure: pointer outside object bounds in values[(signed long int)index]`（第 9 行），反例 `count = 6, index = 6`（即 `index == count`）；另有 harness 後置條件 `rc == ARRAY_LOOKUP_ERR_RANGE`、`out_cell == out_before` 失敗 |

修復後：CBMC `VERIFICATION SUCCESSFUL`（`count <= 8`、`index` 為任意 `size_t`、迴圈全部展開），
5 個 `LEANC_REACH` 探針（`after_call`、`null_input`、`out_of_range`、`index_equals_count`、`in_range`）皆可達；
4 個測試在 ASan/UBSan/LSan 下全部通過；分析器無發現。
