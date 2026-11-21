# 規格格式（`leanc_spec: 1`）

規格是 YAML（或 JSON）檔。所有路徑都相對於**專案根目錄**（最近的含 `leanc.yaml` 的上層目錄，
否則為規格檔所在目錄），且不得為絕對路徑、不得含 `..`、不得為符號連結。未知欄位與重複鍵一律拒絕。
完整範例：[`examples/safe_array_get/spec.yaml`](../examples/safe_array_get/spec.yaml)。

```yaml
leanc_spec: 1                       # schema 版本
spec_id: safe-array-get             # ^[a-z0-9][a-z0-9._-]{0,63}$
version: 1                          # 內容改變後重新批准時必須遞增
title: Safe integer array read
requirement: |                      # 原始自然語言需求（僅供參考，不被任何驗證器使用）
  實作一個安全的整數陣列讀取函式……
language: {name: c, standard: c17}  # c17 | c11 | c99

target:                             # 待驗證函式的介面
  function: safe_array_get
  signature: "int safe_array_get(const int *arr, size_t len, size_t idx, int *out)"
  params:   [{name: arr, type: "const int *", description: "..."}]
  returns:
    type: int
    values: [{value: "SAFE_ARRAY_OK (0)", meaning: "..."}]   # 所有回傳狀態必須有明確定義

contract:                           # 人類可讀的契約；對應的證明義務寫在 harness 中
  preconditions:  [{id: PRE1, text: "...", c_expr: "len <= MAX_LEN"}]
  postconditions: [{id: POST1, text: "..."}]
  safety:         [{id: SAFE1, text: "no out-of-bounds access", checks: [bounds, pointer]}]
  frame:          ["arr[0..len) is never modified"]

sources:
  editable: [src/safe_array.c]      # 生成 / 修復可以寫的檔案（只能是 .c）
  headers:  [include/safe_array.h]  # 受保護的介面標頭
  include_dirs: [include]

compile:
  compiler: clang
  flags: [-Wall, -Wextra, -Wpedantic]
  defines: []
  warnings_policy: report           # report | block（block：有 warning 即 COMPILE_ERROR）
  timeout_s: 60

static_analysis:
  policy: block                     # block：有發現即 STATIC_ANALYSIS_WARNING；report：僅記錄
  extra_checkers: []                # 例如 [alpha.security.ArrayBoundV2]
  timeout_s: 120

tests:                              # 可信測試（受保護）
  driver: tests/test_safe_array_get.c   # 含 main(argc, argv)，依 argv[1] 執行案例
  cases:
    - {name: normal, args: [normal], expect_exit: 0}
    - {name: echo, args: [echo], stdin: "abc", expect_stdout: "abc"}
  sanitizers: [address, undefined, leak]
  timeout_s: 10
  memory_mb: 1024                   # ASan hard_rss_limit_mb
  max_output_bytes: 1048576

verification:                       # CBMC
  harness: harness/safe_array_get_harness.c   # 受保護
  checks: [unsigned-overflow, conversion]     # 額外檢查（強制檢查永遠開啟，見下）
  unwind: 9
  unwindset: {}                     # 例如 {"sum_array.0": 17}
  input_bounds:                     # 記錄用：寫入報告的驗證範圍；實際限制在 harness 的 LEANC_ASSUME
    - {name: len, min: "0", max: "8"}
  malloc_may_fail: false            # true：malloc 可回傳 NULL
  object_bits: null
  vacuity_check: true               # false → 結果只能是 UNKNOWN
  coverage: false                   # true：額外執行 --cover location 並報告未覆蓋行
  timeout_s: 300
  memory_mb: 4096

gates: [compile, static_analysis, sanitizer, cbmc]   # 必要驗證關卡；沒有 cbmc 時最多 UNKNOWN
repair: {max_rounds: 5, max_changed_lines: 200}
provenance: {origin: human}         # human | ai-draft（AI 草案）
```

## V2：C++20、Frama-C/WP、Lean 4

```yaml
language: {name: cpp, standard: c++20}     # C++17/20；editable 為 .cpp/.cc/.cxx，標頭建議 .hpp
gates: [compile, static_analysis, sanitizer] # C++ 不能使用 cbmc / wp（沒有可用的 C++ model checker）→ 最佳狀態 UNKNOWN
```

```yaml
gates: [compile, static_analysis, sanitizer, cbmc, wp]
wp:                                        # Frama-C/WP（只支援 C）
  provers: [alt-ergo, z3]                  # Why3 prover 名稱
  prover_timeout_s: 10                     # 每個目標；smoke test 也會用滿一次 timeout，請保持小值
  timeout_s: 600
  memory_mb: 4096
  require_termination: false               # true：每個迴圈都需要 loop variant，並證明終止
```

ACSL 契約寫在**受保護標頭**中目標函式的原型前（`/*@ requires ... assigns ... ensures ... */`），因此屬於已批准規格。
`-wp-rte`（執行期錯誤守衛）與 smoke test 一律開啟、不能關閉；WP 快取一律停用。可編輯的 C 原始碼只能包含：
`loop invariant` / `loop assigns` / `loop variant` / `assert` / `check`、static helper 的契約（緊接在其定義之前）、
有本體的非遞迴 `predicate`/`logic`、`lemma`。禁止：目標函式上的任何契約（Frama-C 會與批准的契約**合併**）、
`requires \false`、`admit`、`axiom`/`axiomatic`、`inductive`、`ghost`、在註解中使用本檔定義的巨集、`#define`
批准標頭 ACSL 中用到的識別字或 `integer`/`real`/`boolean`。

```yaml
gates: [compile, static_analysis, sanitizer, cbmc, lean]
lean:                                      # Lean 4（可選；需要 lean 與 leanchecker）
  spec_file: lean/Spec.lean                # 受保護：模型 + 定理敘述（每個 `def <name> : Prop := ...`）
  proofs: [lean/Proofs.lean]               # 可編輯：AI 撰寫的證明
  theorems:
    - {name: in_bounds, statement: Spec.clamp_in_bounds, proof: Proofs.clamp_in_bounds_proof}
  vectors:                                 # 可選：模型 ↔ C 的差分測試
    generator: lean/Vectors.lean           # 受保護：以模型計算輸入/期望輸出（`lean --run`）
    driver: tests/vectors_driver.c         # 受保護：從 stdin 讀取向量並檢查 C 實作
  timeout_s: 300
```

Lean 關卡在沙箱中以 kernel 檢查證明（`Elab.async false`），以 `leanchecker` 重新 replay，且只接受
`propext`、`Classical.choice`、`Quot.sound` 三個公理；`sorry`、`native_decide`、自訂 axiom 都會使定理「未證明」。
向量 driver 必須在成功時印出恰好一行 `LEANC-VECTORS-CHECKED <n>`，不符時印 `LEANC-VECTOR-MISMATCH k=v ...`。
Lean 結果是關於**模型**的，不會把 C 的判決提升為 `PROVED`。

## CBMC 檢查類別

| 名稱 | CBMC 旗標 | 強制 |
|---|---|---|
| `bounds` | `--bounds-check` | ✔ |
| `pointer` | `--pointer-check` | ✔ |
| `div-by-zero` | `--div-by-zero-check` | ✔ |
| `signed-overflow` | `--signed-overflow-check` | ✔ |
| `pointer-overflow` | `--pointer-overflow-check` | ✔ |
| `undefined-shift` | `--undefined-shift-check` | ✔ |
| `unsigned-overflow` | `--unsigned-overflow-check` | |
| `conversion` | `--conversion-check` | |
| `float-overflow` | `--float-overflow-check` | |
| `nan` | `--nan-check` | |
| `memory-leak` | `--memory-leak-check` | |
| `pointer-primitive` | `--pointer-primitive-check` | |
| `enum-range` | `--enum-range-check` | |

強制檢查（`MANDATORY_CHECKS`）無法被規格關閉；`--unwinding-assertions` 永遠開啟。
規格中沒有「任意額外 CBMC 參數」欄位，避免以旗標削弱驗證。

## Harness 撰寫規範

```c
#include "leanc.h"          /* LEANC_ASSUME, LEANC_REACH, nondet_* 宣告 */
#include "safe_array.h"

int main(void) {
    size_t len = nondet_size_t();
    LEANC_ASSUME(len <= 8);                 /* 前置條件：不要過度限制！ */
    ...
    int rc = safe_array_get(arr, len, idx, out);
    LEANC_REACH("after_call");              /* vacuity 探針：必須可達 */
    if (...) { LEANC_REACH("in_range"); assert(rc == 0 && *out == arr[idx]); }
    return 0;
}
```

* 每個分支至少一個 `LEANC_REACH`，呼叫之後至少一個。任何探針不可達 → `UNKNOWN`（vacuous）。
* 後置條件要「強」：檢查實際值（`*out == arr[idx]`），不只是回傳碼範圍，避免「固定回傳值」式修復。
* `LEANC_ASSUME` 只表達真正的前置條件；過強的 assume 會讓驗證失去意義（`coverage: true` 可協助發現）。
* harness 只能由人撰寫/審查；可編輯原始碼中禁止出現 `__CPROVER_*` / `LEANC_*`。

## 專案設定 `leanc.yaml`（可選）

```yaml
provider: mock        # 預設 LLM provider：mock | openai
mock_dir: mock
sandbox: auto         # auto | none
allow_unsandboxed: false
```

設定檔不能改變檢查、界限、關卡或批准規則。

## LLM provider 環境變數

| 變數 | 說明 |
|---|---|
| `LEANC_API_KEY`（或 `OPENAI_API_KEY`） | API key（不會寫入日誌/報告；子行程看不到） |
| `LEANC_BASE_URL` | OpenAI 相容端點，預設 `https://api.openai.com/v1` |
| `LEANC_MODEL` | 模型名稱（必填） |
| `LEANC_LLM_TIMEOUT` | 請求逾時秒數 |
| `LEANC_LLM_JSON_MODE=1` | 送出 `response_format: json_object` |
| `LEANC_HOME` | 批准金鑰目錄（預設 `~/.config/leanc`） |
| `LEANC_CLANG` / `LEANC_CBMC` | 指定工具路徑 |
| `LEANC_CLANGXX` / `LEANC_FRAMAC` / `LEANC_LEAN` | 指定 clang++、frama-c（需 ≥ 27，含 WP）、lean 的路徑 |
