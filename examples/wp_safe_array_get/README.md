# wp_safe_array_get — 以 Frama-C/WP 演繹證明（V2，`PROVED`）

需求與程式碼與 `examples/safe_array_get` 相同：「實作一個安全的整數陣列讀取函式。當索引超出範圍時，
必須回傳錯誤，而不是讀取非法記憶體。」差別在於受保護的標頭檔 `include/safe_array.h` 在
`safe_array_get` 的原型上附帶 **ACSL 合約**，規格多了 `wp` 關卡。

| 檔案 | 角色 | 信任 |
|---|---|---|
| `spec.yaml` | 結構化規格（含 `wp:` 區段：證明器、每個目標的逾時、`require_termination: true`） | 批准後鎖定 |
| `include/safe_array.h` | 介面 + **已批准的 ACSL 合約**（`requires`/`assigns`/`ensures`、三個 behavior：`null_input`、`out_of_range`、`in_range`，`complete`/`disjoint behaviors`） | 受保護 |
| `harness/safe_array_get_harness.c` | CBMC harness（與 V1 相同，`len <= 8`） | 受保護 |
| `tests/test_safe_array_get.c` | 可信任測試（ASan/UBSan/LSan） | 受保護 |
| `src/safe_array.c` | 實作（AI 可生成/修復；不含任何 ACSL） | 可編輯 |
| `mock/generate/src/safe_array.c` | Mock LLM「生成」的程式（與出貨版相同，非真實 LLM 表現） | — |

ACSL 合約把文件中的行為寫成可證明的形式：

* `arr == \null || out == \null` ⇒ 回傳 `SAFE_ARRAY_ERR_NULL`，且 `assigns \nothing`；
* 非 NULL 且 `idx >= len` ⇒ 回傳 `SAFE_ARRAY_ERR_RANGE`，`*out == \old(*out)`（錯誤時不改動）；
* 非 NULL 且 `idx < len` ⇒ 回傳 `SAFE_ARRAY_OK`，`*out == \old(arr[idx])`；
* 前置條件：`arr` 指向 `len` 個可讀的 int（`\valid_read`）、`out` 可寫、`out` 不指向 `arr` 內部（`\separated`）；
  `assigns *out` 表示陣列永不被修改。

```bash
leanc spec approve examples/wp_safe_array_get/spec.yaml --by alice --yes
leanc verify examples/wp_safe_array_get/spec.yaml        # => PROVED (exit 0)
leanc run examples/wp_safe_array_get/spec.yaml --provider mock --regenerate   # => PROVED（不需修復）
```

`PROVED` 的範圍（報告的 assumptions 會逐條列出）：

* Frama-C/WP 對**所有**滿足前置條件的輸入（任意 `len`、任意 `idx`，沒有 CBMC 那樣的界限）證明了已批准的
  ACSL 合約，以及 `-wp-rte` 產生的所有執行期錯誤防護（記憶體存取、溢位、指標運算……）；
* 所有 smoke test 通過（沒有矛盾的前置條件、永不成立的 behavior 或死碼）；
* `require_termination: true`，終止性也已證明（全正確性）；
* 前提：WP 的 Typed 記憶體模型、Frama-C 的 x86_64 機器模型、Frama-C libc 規格，以及列出的證明器
  （Alt-Ergo、Z3）。它證明的是 Frama-C 解析的 C 原始碼，不涵蓋編譯器的正確性。

`PROVED` 只可能來自 `wp` 關卡，而且其他所有必要關卡（compile、integrity、static analysis、sanitizer、
CBMC）也都必須通過。
