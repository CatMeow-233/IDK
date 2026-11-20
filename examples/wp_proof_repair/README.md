# wp_proof_repair — 證明修復：補上迴圈不變式（V2）

需求：「實作 `index_of_max(a, n)`：回傳陣列 `a[0..n)` 中『第一個』最大元素的索引（`n >= 1`），不得修改陣列。」

出貨的 `src/array_max.c` **是正確的**：編譯、integrity、靜態分析、sanitizer 測試與 CBMC（`n = 1..5`、任意值）
全部通過。但 Frama-C/WP 無法證明一個**沒有迴圈不變式**的迴圈：WP 只知道迴圈條件，因此後置條件
`in_bounds` / `is_max` / `is_first`、`a[i]` 與 `a[best]` 的 4 個 RTE 防護、`assigns \nothing`
（迴圈沒有 `loop assigns`，WP 只好假設它可能寫入任何東西）以及終止性（沒有 `loop variant`）共 9 個目標未被證明。

所以 `leanc verify` 的結果是 **`UNKNOWN`**——不是反例，也不是成功。因為除了 `wp` 之外的必要關卡都通過、
`wp` 只是 INCONCLUSIVE，這個結果是 **proof-repairable**：修復代理可以只加入「證明註解」。

| 檔案 | 角色 | 信任 |
|---|---|---|
| `spec.yaml` | 結構化規格（`wp:` 區段，`require_termination: true`） | 批准後鎖定 |
| `include/array_max.h` | 介面 + **已批准的 ACSL 合約**（`requires n > 0`、`\valid_read`、`assigns \nothing`、三個具名 `ensures`） | 受保護 |
| `harness/array_max_harness.c` | CBMC harness（與合約相同的後置條件 + 陣列不變） | 受保護 |
| `tests/test_array_max.c` | 可信任測試 | 受保護 |
| `src/array_max.c` | 出貨的實作（正確，但沒有迴圈不變式） | 可編輯 |
| `mock/generate/src/array_max.c` | Mock「生成」的程式（與出貨版相同） | — |
| `mock/repair/round-1.json` | 第 1 輪：界限不變式 `1 <= i <= n`、`0 <= best < i` 與 `loop assigns i, best` | — |
| `mock/repair/round-2.json` | 第 2 輪：功能不變式（`a[0..i)` 都 `<= a[best]`；`best` 之前都嚴格較小）與 `loop variant n - i` | — |

```bash
leanc spec approve examples/wp_proof_repair/spec.yaml --by alice --yes
leanc verify examples/wp_proof_repair/spec.yaml                          # => UNKNOWN (exit 20)
leanc run examples/wp_proof_repair/spec.yaml --provider mock --regenerate # => PROVED（2 輪證明修復）
```

修復迴圈如何判斷「進步」：第 1 輪之後仍是 `UNKNOWN`，但未證明的目標從 9 個降到 3 個（`is_max`、`is_first`、
終止性），新加入的不變式本身（established / preserved）全部被證明、沒有引入新的未證明目標，所以被接受；
第 2 輪之後所有目標（含終止性）都被證明 ⇒ `PROVED`。可執行程式碼從頭到尾沒有改變。

信任規則：可編輯程式碼中的 ACSL 只能是 `loop invariant` / `loop assigns` / `loop variant` / `assert` /
`check`，或非目標 static 輔助函式的合約——這些都是 WP 必須證明的義務，而不是假設。`admit`、`axiom`、
在目標函式上另加合約（Frama-C 會與已批准的合約合併）等會被 integrity 關卡與修復政策拒絕。
