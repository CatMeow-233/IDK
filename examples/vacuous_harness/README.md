# vacuous_harness — 空洞（vacuous）的 harness

需求：「實作一個安全的整數陣列讀取函式。當索引超出範圍時，必須回傳錯誤，而不是讀取非法記憶體。」

`src/array_read.c` 有真正的 off-by-one 缺陷（`idx > len` 應為 `idx >= len`，`idx == len` 時越界讀取）。
但已批准的 harness 有兩個各自看似合理、合起來卻**互相矛盾**的假設：

```c
LEANC_ASSUME(len >= MIN_LEN);   /* MIN_LEN = 8，「只測非平凡陣列」 */
LEANC_ASSUME(len <= MAX_LEN);   /* MAX_LEN = 4，儲存空間大小 */
```

沒有任何執行能到達函式呼叫，因此單純執行 `cbmc` 會印出 **VERIFICATION SUCCESSFUL** ——
所有 assert 都「成立」，但這是空洞的成立。Lean C 以 `-DLEANC_VACUITY_CHECK` 重跑 CBMC，
每個 `LEANC_REACH` 探針變成 `__CPROVER_assert(0, "leanc_reach:…")`：CBMC 報告探針 SUCCESS
（= 不可達），於是結果是 `UNKNOWN`（vacuous），而不是 `VERIFIED_BOUNDED`。

| 檔案 | 角色 | 信任 |
|---|---|---|
| `spec.yaml` | 規格（gates 只有 `compile`, `cbmc`，以隔離「只靠 CBMC」的結論） | 批准後鎖定 |
| `spec_fixed_harness.yaml` | 變體（`spec_id: vacuous-harness-fixed`）：使用修正後的 harness | 批准後鎖定 |
| `include/array_read.h` | 介面 | 受保護 |
| `harness/array_read_harness.c` | **有矛盾假設**的 harness | 受保護 |
| `harness/array_read_harness_fixed.c` | 移除 `len >= MIN_LEN` 後的 harness | 受保護 |
| `src/array_read.c` | 實作（含 off-by-one） | 可編輯 |
| `mock/generate/` | Mock LLM 固定輸出（與 `src/` 相同；非真實 LLM 表現） | — |

```bash
leanc spec approve examples/vacuous_harness/spec.yaml --by alice --yes
leanc verify examples/vacuous_harness/spec.yaml              # => UNKNOWN (exit 20): vacuous
leanc run examples/vacuous_harness/spec.yaml --provider mock # => UNKNOWN，0 輪修復

leanc spec approve examples/vacuous_harness/spec_fixed_harness.yaml --by alice --yes
leanc verify examples/vacuous_harness/spec_fixed_harness.yaml  # => COUNTEREXAMPLE_FOUND (exit 13)
```

實測（CBMC 5.95.1）：

| 執行 | 結果 |
|---|---|
| `cbmc`（原 harness，含所有檢查） | `VERIFICATION SUCCESSFUL`，0/37 個性質失敗 |
| `cbmc -DLEANC_VACUITY_CHECK`（原 harness） | `after_call`, `null_input`, `out_of_range`, `in_range` 皆 SUCCESS = **不可達** |
| `cbmc`（修正後 harness） | `array_read.pointer_dereference`：`pointer outside object bounds in arr[idx]`（`src/array_read.c:11`），反例 `len=4, idx=4`；以及 harness 的 `rc == ARRAY_READ_ERR_RANGE`、`out_cell == out_before`（`len=1, idx=1`） |

重點：`UNKNOWN` 不可修復 —— 錯的是已批准的 harness，修正它需要人類審查並批准新規格；
修復代理不會被呼叫（也不能修改 harness）。
