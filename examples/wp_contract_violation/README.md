# wp_contract_violation — 違反 ACSL 後置條件（V2）

需求：「實作 `find_first(a, n, key, pos)`：在 `a[0..n)` 中尋找 `key` 的第一個出現位置。找到時回傳 1 並將索引
寫入 `*pos`；找不到時回傳 0 且不修改 `*pos`。不得修改陣列。」

**缺陷**（`src/find_first.c`，即 mock「生成」的程式）——迴圈少看最後一個元素：

```c
for (size_t i = 0; i + 1 < n; i++) {   /* 應為 i < n */
```

當 `key` 只出現在 `a[n-1]` 時，函式回傳 0，違反受保護標頭檔中 ACSL 合約 `behavior found` 的
`ensures \result == 1`。

| 檔案 | 角色 | 信任 |
|---|---|---|
| `spec.yaml` | 結構化規格（`wp:` 區段，`require_termination: true`） | 批准後鎖定 |
| `include/find_first.h` | 介面 + **已批准的 ACSL 合約**（behavior `found` / `not_found`，`complete`/`disjoint`，`assigns *pos`） | 受保護 |
| `harness/find_first_harness.c` | CBMC harness：斷言與合約**相同**的後置條件（`n <= 4`、任意值與 key） | 受保護 |
| `tests/test_find_first.c` | 可信任測試（沒有把 key 放在最後一個位置——測試漏掉了這個缺陷） | 受保護 |
| `src/find_first.c` | 出貨的有缺陷實作（附有對這個迴圈而言正確的迴圈不變式） | 可編輯 |
| `mock/repair/round-1.json` | 修復：迴圈界限改為 `i < n`（不變式不變） | — |

```bash
leanc spec approve examples/wp_contract_violation/spec.yaml --by alice --yes
leanc verify examples/wp_contract_violation/spec.yaml                          # => COUNTEREXAMPLE_FOUND (exit 13)
leanc run examples/wp_contract_violation/spec.yaml --provider mock --regenerate # => PROVED（1 輪修復）
```

誰發現了缺陷：

* **CBMC** 給出具體反例（例如 `n = 3`、key 只在 `a[2]`），harness 的 `assert(rc == 1)` 失敗 ⇒ `COUNTEREXAMPLE_FOUND`；
* **Frama-C/WP** 無法證明 `behavior found` 的 3 個後置條件（以及 `i + 1` 的無號溢位防護）。WP 不會把未證明的目標
  當成反例：它們是 INCONCLUSIVE（`FailureReport.kind = "unproved"`）；
* sanitizer 測試與靜態分析都沒有發現（測試沒有涵蓋這個情況）。

整體狀態取最差者：`COUNTEREXAMPLE_FOUND`。修復後 CBMC 通過，WP 證明所有目標（含終止性與 smoke test）⇒ `PROVED`。
