# safe_array_get — 正確程式 (Test 1)

需求：「實作一個安全的整數陣列讀取函式。當索引超出範圍時，必須回傳錯誤，而不是讀取非法記憶體。」

| 檔案 | 角色 | 信任 |
|---|---|---|
| `spec.yaml` | 結構化規格（前置/後置條件、安全性質、界限、關卡） | 批准後鎖定 |
| `include/safe_array.h` | 介面（回傳碼） | 受保護 |
| `harness/safe_array_get_harness.c` | CBMC harness：符號輸入、`LEANC_ASSUME` 前置條件、後置條件 assert、`LEANC_REACH` 探針 | 受保護 |
| `tests/test_safe_array_get.c` | 可信任測試（ASan/UBSan/LSan 下執行） | 受保護 |
| `src/safe_array.c` | 實作（AI 可生成/修復） | 可編輯 |
| `mock/` | Mock LLM 固定輸出（離線示範用，非真實 LLM 表現） | — |

在 `leanc init` 建立的專案目錄（Lean C 原始碼庫中為 `examples/safe_array_get/`）下執行：

```bash
leanc spec approve spec.yaml --by alice --yes
leanc verify spec.yaml        # => VERIFIED_BOUNDED (exit 0)
```

`VERIFIED_BOUNDED` 的範圍：`len <= 8`（MAX_LEN）、`idx` 為任意 `size_t`、`arr`/`out` 可為 NULL，
迴圈全部展開（unwinding assertions 通過），所有探針可達（非 vacuous），檢查類別見報告。
