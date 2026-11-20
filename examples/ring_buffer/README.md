# ring_buffer — 多檔案專案：環形緩衝區（正確程式）

需求：「實作一個固定容量的整數環形緩衝區（FIFO）：初始化、推入、取出，以及統計函式。滿時推入與空時取出必須
回傳錯誤且不改變內容；任何操作序列都必須維持先進先出順序，且不得越界存取。」

本例示範**多個可編輯來源檔**共用一個受保護標頭：生成、編譯（逐檔）、靜態分析（逐檔）、sanitizer 測試與
CBMC 都把兩個 `.c` 檔一起處理；修復代理也只能改這兩個檔。

| 檔案 | 角色 | 信任 |
|---|---|---|
| `spec.yaml` | 規格（target `ring_push`；契約涵蓋全部 8 個函式） | 批准後鎖定 |
| `include/ring.h` | 介面與表示不變量（`struct ring`） | 受保護 |
| `harness/ring_harness.c` | CBMC harness：容量 1..3（另含被拒絕的 0 與 > `RING_MAX_CAPACITY`）、5 步任意 push/pop 序列、以線性陣列模型檢查不變量、FIFO 順序、所有觀察函式 | 受保護 |
| `tests/test_ring.c` | 可信任測試：FIFO、滿/空、多次 wraparound（剛好大小的 heap 儲存區，ASan 可抓越界）、統計、錯誤參數 | 受保護 |
| `src/ring.c` | `ring_init`, `ring_push`, `ring_pop` | 可編輯 |
| `src/ring_stats.c` | `ring_count`, `ring_is_empty`, `ring_is_full`, `ring_peek`, `ring_sum` | 可編輯 |
| `mock/generate/src/*.c` | Mock 生成（與 `src/` 相同；非真實 LLM 表現） | — |

```bash
leanc spec approve examples/ring_buffer/spec.yaml --by alice --yes
leanc verify examples/ring_buffer/spec.yaml               # => VERIFIED_BOUNDED (exit 0)
leanc run examples/ring_buffer/spec.yaml --provider mock  # => VERIFIED_BOUNDED (0 repair rounds)
```

`VERIFIED_BOUNDED` 的範圍：容量 ≤ 3、長度 5 的所有操作序列、任意 `int` 值，`--unwind 6` 且 unwinding
assertions 通過；8 個探針（`init_null`, `init_bad_capacity`, `push_ok`, `push_full`, `pop_ok`, `pop_empty`,
`wrapped`, `after_ops`）全部可達。更大的容量或更長的序列不在保證範圍內（由測試補充，但測試不是證明）。

Harness 強度的抽查（mutation）：把 `ring_push` 的 `tail >= capacity` 改成 `>`，或把 `ring_stats.c` 中
`slot()` 的 `idx >= capacity` 改成 `>`，CBMC 都會回報反例（FIFO/peek/sum 斷言失敗與 `r->buf[...]` 越界）。

CBMC 成本（CBMC 5.95.1，本機）：主驗證約 4–5 s，vacuity 約 2–3 s。
