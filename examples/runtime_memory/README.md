# runtime_memory — 只有執行期 sanitizer 才抓得到的記憶體洩漏

需求：「實作一個函式，把兩個欄位以分隔字元串接成新配置的字串（呼叫者負責釋放）。若欄位含有分隔字元或
NUL 字元、參數為 NULL 或欄位過長，必須回傳錯誤、輸出清空，且不得遺留任何已配置的記憶體。」

`src/join.c` 把 `malloc` 的緩衝區直接存進輸出參數 `res->data`；當欄位含有分隔字元或 NUL 時，
跳到共用的 `fail:` 標籤，把 `res->data` 設為 `NULL` —— **卻沒有 `free`**。

| 工具 | 是否發現 | 原因 |
|---|---|---|
| clang 編譯 | 否 | 無警告 |
| Clang Static Analyzer | 否 | 指標經由輸出參數逃逸（escape），analyzer 不再追蹤這塊記憶體 |
| CBMC（規格的檢查：bounds, pointer, …, unsigned-overflow；**沒有** memory-leak） | 否 | 記憶體安全與功能後置條件皆成立，6 個探針全部可達 |
| ASan/UBSan/**LSan**（可信任測試） | **是** | `separator_in_field`、`nul_in_field`：`Direct leak of 6 byte(s) … allocated from join_with_sep src/join.c:22` |

（對照：若規格加入 `memory-leak` 檢查，CBMC 也會報告 `__CPROVER_start.memory-leak.1: FAILURE`。
本例刻意不開，以示範各 gate 的互補：洩漏屬於規格的 POST6/SAFE4，由 sanitizer gate 負責。）

| 檔案 | 角色 | 信任 |
|---|---|---|
| `spec.yaml` | 規格（POST5/POST6：錯誤時清空輸出且不得洩漏） | 批准後鎖定 |
| `include/join.h` | 介面（`struct joined`、回傳碼） | 受保護 |
| `harness/join_harness.c` | CBMC harness：欄位長度 0..3（符號內容）或 > 4096（不讀取）、任意分隔字元、NULL 輸入 | 受保護 |
| `tests/test_join.c` | 可信任測試；成功結果一律由測試釋放，故任何洩漏都來自 `join_with_sep` | 受保護 |
| `src/join.c` | 實作（錯誤路徑洩漏） | 可編輯 |
| `mock/generate/` | Mock 生成（與 `src/` 相同） | — |
| `mock/repair/round-1.json` | Mock 修復：在 `fail:` 中先 `free(res->data)` | — |

```bash
leanc spec approve examples/runtime_memory/spec.yaml --by alice --yes
leanc verify examples/runtime_memory/spec.yaml               # => RUNTIME_TEST_FAILED (exit 12), detected by lsan
leanc run examples/runtime_memory/spec.yaml --provider mock  # => VERIFIED_BOUNDED after 1 repair round
```

Mock 輸出是固定的示範資料，不代表任何 LLM 的表現。
