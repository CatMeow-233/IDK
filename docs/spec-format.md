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
    values: [{value: "SAFE_ARRAY_OK (0)", meaning: "..."}]   # 建議列出所有回傳狀態（只用於 LLM 提示；驗證器不檢查）

contract:                           # preconditions / postconditions / frame 供人閱讀（證明義務寫在 harness 中）；safety[].checks 會加入 CBMC 檢查
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
  malloc_may_fail: false            # true：malloc 可回傳 NULL（不能與 inductive 關卡一起使用）
  object_bits: null
  vacuity_check: true               # false → 結果只能是 UNKNOWN
  coverage: false                   # true：額外執行 --cover location 並報告未覆蓋行
  timeout_s: 300
  memory_mb: 4096

gates: [compile, static_analysis, sanitizer, cbmc]   # 必要驗證關卡；沒有 cbmc 也沒有 wp 時最多 UNKNOWN；opt-in 的 inductive 見下文
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

## Inductive profile（opt-in，只支援 C）

完整範例：[`examples/inductive_sponge`](../examples/inductive_sponge/README.md)。

```yaml
inductive:                                  # 可選；沒有這個區段時，規格的正規形式與雜湊不變
  harness: harness/sponge_inductive.c       # 受保護：含 main() 的 inductive harness
  contracts: inductive/loops.json           # 可編輯：迴圈契約檔（.json）；檔案不存在 = 沒有契約
  functions: [sponge_absorb]                # claimed 函式：不重複的 C 識別字，必須包含 target.function
gates: [compile, static_analysis, sanitizer, cbmc, inductive]
```

* 沒有設定的 `inductive` 區段不會出現在規格的正規形式中（`specs/loader.py` 的
  `SECTIONS_ABSENT_FROM_CANONICAL_FORM_WHEN_UNSET`），所以沒有這個區段的規格，雜湊、批准與判定都不變；加入區段會改變雜湊，
  需要重新批准。
* `harness` 是受保護檔（批准時雜湊鎖定，生成與修復不能寫）。`contracts` 必須以 `.json` 結尾、不能在 `.leanc/` 內；它是可編輯檔
  （修復可以寫，`generate` 不寫，不計入批准的雜湊），規格檔本身不能同時是契約檔（批准會拒絕）。`functions` 至少一項、不可重複，
  必須包含 `target.function`；每個 claimed 函式在可編輯原始碼中恰好有一個非 static 的定義，並且被 harness 直接呼叫。
* 關卡 `inductive` 需要 `inductive` 區段與 `cbmc` 關卡（`VERIFIED_INDUCTIVE` 需要 `cbmc` PASS）。
  `verification.malloc_may_fail: true` 會被規格驗證器拒絕（本版本不支援：malloc 失敗旗標只在 DFCC 步驟有作用，交給之後的 cbmc 會被忽略），
  關卡本身遇到它也回報 `ERROR`。
* 關卡也使用 `verification` 區段的 `unwind`（harness 與函式庫迴圈的展開界限，至少為最長的 assigns 清單 + 2）、`object_bits`
  （同時交給 goto-cc 與 cbmc）、`timeout_s` 與 `memory_mb`（每一次工具執行的上限，乘上 `--timeout-scale`；逾時為 `TIMEOUT`）。
* 需要 `cbmc`、`goto-cc`、`goto-instrument`（`LEANC_CBMC`、`LEANC_GOTO_CC`、`LEANC_GOTO_INSTRUMENT`），三者都必須是經過測試的
  CBMC 5.95.1（`toolchain/cprover.py` 的 `TESTED_INDUCTIVE_CBMC`），否則關卡為 `ERROR`。

### 契約檔（`inductive.contracts`）

```json
{"loops": [
  {"file": "src/sponge.c", "line": 35,
   "invariant": ["i <= len", "s->fill < 144"],
   "assigns": ["i", "__CPROVER_object_whole(s)"],
   "decreases": "len - i"}
]}
```

* 嚴格 JSON：重複鍵、多餘鍵、缺少鍵與錯誤型別一律拒絕（規則 `LOOP_CONTRACT`，關卡回報 `contract_rejected`）。五個鍵都必填：
  `file` 必須是 `sources.editable` 中的檔案；`line`（1 到 10,000,000 的整數）是 `for` / `while` 關鍵字所在的原始行；
  `invariant` 與 `assigns` 各 1 到 16 項；`decreases` 必填（沒有 decreases 時，不終止的迴圈也會通過）。`(file, line)` 不可重複，
  最多 64 個迴圈，整個檔案最多 1,048,576 個字元。
* 契約的位置：在該檔案的 `cbmc --preprocess` 輸出中，linemarker 來源為該行的 `for` / `while` / `do` / `goto` 關鍵字必須恰好一個；
  它必須是 `for` 或 `while`、寫在該原始行上（不是巨集展開出來的迴圈）、`( )` 標頭在同一檔案中結束、本體是 `{` 區塊
  （不是 `;`，也不是 `<%`）。其他情況都是 `contract_rejected`，從不猜測。
* claimed 呼叫會到達的每個迴圈（harness 與 CBMC 函式庫之外）都需要契約：DFCC 之後仍沒有契約的迴圈是 `loop_without_contract`，
  CBMC 不會執行。harness 走不到的迴圈的契約是 `contract_rejected`（請移除它）。
* 契約只是提案：CBMC 證明 invariant 在進入時成立、從任意滿足 invariant 的狀態走一步後仍成立、迴圈只寫入 assigns 列出的物件
  （包括被呼叫函式的寫入）、decreases 每步嚴格遞減；沒有任何子句被假設。

### 子句文法

每個子句以白名單文法解析（`leanc/lang/c_expr_lexer.py`、`leanc/lang/c_expr.py`），只有完全加括號的呈現結果被插入
`build/` 下的 `cbmc --preprocess` 輸出；原始文字從不交給工具。

* 允許：識別字（區域變數、參數、全域變數、成員名稱）；整數常數（十進位、十六進位、八進位，可加 `u` / `l` / `ll` 後綴；
  有號十進位最大 2^63 − 1，其他最大 2^64 − 1）；二元運算子 `||`、`&&`、`|`、`^`、`&`、`==`、`!=`、`<`、`<=`、`>`、`>=`、`<<`、
  `>>`、`+`、`-`、`*`、`/`、`%`；一元 `-`、`!`、`~`、`*`；成員 `.` / `->`、索引 `[ ]`、括號；轉型到整數型別（`int`、
  `unsigned long`、`size_t`、`uint32_t`…）。
* `invariant` 中可用 `__CPROVER_loop_entry(<lvalue>)`（不可巢狀）。`assigns` 的每一項是 lvalue（名稱、成員、索引、`*p`），
  或 `__CPROVER_object_whole(<指標>)`、`__CPROVER_object_upto(<指標>, <位元組數>)`（逗號只能出現在這裡）。這三個內建名稱不能
  出現在其他位置。
* 拒絕：函式呼叫、指定與複合指定、`++` / `--`、`?:`、逗號運算子、取址 `&`、一元 `+`、`sizeof` 與其他 C 關鍵字、量詞
  （`__CPROVER_forall`、`__CPROVER_exists`）、大括號（statement expression、compound literal、區塊）、註解、digraph、浮點常數、
  字串與字元常值、轉型以外的型別名稱與非整數轉型、保留識別字（`__*`、`_[A-Z]*`）、以 `nondet_`、`LEANC_`、`leanc_` 開頭的名稱、
  巨集名稱：`sources.editable` 中 `#define` 的名稱，以及 `NULL`、`true`、`false`、`bool`、`EOF`、`errno`、`CHAR_BIT`、`offsetof`
  與 `INT_MAX`、`SIZE_MAX`、`UINT32_MAX` 等界限巨集。子句不做巨集展開，請直接寫數值（例如 `144` 而不是 `SPONGE_RATE`）；受保護
  標頭中的巨集名稱在插入的位置不會展開，goto-cc 編譯失敗，結果同樣是 `contract_rejected`。只允許可列印 ASCII 與空白。
* 上限：每個子句 500 個字元、256 個記號、巢狀深度 32。

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

強制檢查（`MANDATORY_CHECKS`）無法被規格關閉；實際啟用的檢查 = 強制檢查 + `verification.checks` + `contract.safety[].checks`。
`inductive` 關卡只使用強制檢查（從任意狀態出發，合法的模運算環繞與洩漏沒有意義）；其他類別只有 `cbmc` 關卡的有界結果。
`--unwinding-assertions` 在 verify 與 vacuity 執行中永遠開啟（`coverage: true` 的額外執行不帶，該執行不影響判決），後面緊接 `--no-self-loops-to-assumptions`：跳回自己的迴圈（`for (;;) {}`、`while (1);`）不會被 CBMC 變成假設，在允許的輸入上永不結束時展開斷言失敗（`INCONCLUSIVE`）。
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
* 每個 `nondet_*` 都要有宣告：`leanc.h` 宣告了 `nondet_int`、`nondet_uint`、`nondet_long`、`nondet_ulong`、`nondet_llong`、`nondet_size_t`、`nondet_char`、`nondet_uchar`、`nondet_short`、`nondet_bool`、`nondet_int32_t`、`nondet_uint32_t`、`nondet_int64_t`、`nondet_uint64_t`，其他的（如 `unsigned long long nondet_ulonglong(void);`）在 harness 中宣告。CBMC 把未宣告的函式當成回傳 `int`，輸入只涵蓋 `int` 的值；CBMC 的 `function 'f' is not declared` 警告會使 `cbmc` 關卡為 `INCONCLUSIVE`（`UNKNOWN`）。
* 後置條件要「強」：檢查實際值（`*out == arr[idx]`），不只是回傳碼範圍，避免「固定回傳值」式修復。
* `LEANC_ASSUME` 只表達真正的前置條件；過強的 assume 會讓驗證失去意義（`coverage: true` 可協助發現）。
* harness 只能由人撰寫/審查；可編輯原始碼中禁止出現 `__CPROVER_*` / `LEANC_*`。

### inductive harness（`inductive.harness`）

```c
int main(void)
{
    struct sponge *s = malloc(sizeof(struct sponge));
    LEANC_ASSUME(s != NULL);
    LEANC_ASSUME(s->fill < SPONGE_RATE);          /* 狀態不變量，其餘欄位任意 */
    size_t len = nondet_size_t();
    LEANC_ASSUME(len <= ((size_t)1 << 48));
    uint8_t *data = malloc(len);
    LEANC_ASSUME(data != NULL);
    int rc = sponge_absorb(s, data, len);         /* claimed 呼叫：之後不能再有假設 */
    LEANC_REACH("absorb_returned");
    assert(rc == SPONGE_OK);
    assert(s->fill < SPONGE_RATE);
    free(data);
    free(s);
    return 0;
}
```

* `main()` 只定義一次且在 harness 檔中；它以 `LEANC_ASSUME` 在任意狀態上假設狀態不變量，直接呼叫 claimed 函式，在呼叫之後斷言，
  且至少有一個 `LEANC_REACH`（沒有就是 `ERROR`）。harness 本身可以使用巨集與 `nondet_*`；每個 `nondet_*` 都要宣告
  （goto-cc 以 `--verbosity 2` 執行，`function 'f' is not declared` 警告是 `undeclared_function`，`UNKNOWN`）。
* 每條路徑最多一次 claimed 呼叫（harness 中的呼叫）：Lean C 在 harness 副本的每次 claimed 呼叫前插入
  `__CPROVER_assert(leanc_claimed_calls == 0, "leanc_claimed_call:<f>")`，所以任何 claimed 函式都不會從另一次 claimed 呼叫的結束
  狀態開始。要涵蓋多個函式，就用分支讓每條路徑只呼叫其中一個。違反是 `ERROR`。
* claimed 呼叫之後不能有假設（`LEANC_ASSUME` 或 `__VERIFIER_assume`）：同一函式內位於第一次 claimed 呼叫之後的假設在建置時拒絕，
  其他路徑上的假設由插入的 `leanc_assume_after_claimed_call` 斷言逐路徑檢查；假設中也不能呼叫 claimed 函式。違反是 `ERROR`。
* 探針：vacuity 執行除了 harness 的每個 `LEANC_REACH` 位置，還要到達每個 claimed 函式入口的 `leanc_function:<f>`、每個帶契約迴圈
  本體開頭的 `leanc_loop:<file>:<line>`，以及 harness 中每次 claimed 呼叫返回之後的 `leanc_return:<f>`；任何一個沒到達都是
  `vacuous_harness`（`UNKNOWN`）。
* 其他 `ERROR`：claimed 函式以直接呼叫以外的方式使用（取址、函式指標、同名遮蔽）或從未被呼叫；函式本體中參考 `main`；
  進入 claimed 函式之前執行了任何可編輯函式（假設中的可編輯 predicate、setup 呼叫、claimed 呼叫的引數；每個可編輯函式都會被插入
  入口檢查）；`main()` 中除了唯一一個位於最後的頂層 `return` 以外的 `return`，或 `main()` 中的 `goto`；`main()` 中從最後一個
  `LEANC_ASSUME`（沒有就從開頭）到第一次直接 claimed 呼叫結束之間的 `=`、複合指定、`++`、`--`（一般宣告 `T x = ...;` 的 `=` 除外）；
  harness 翻譯單元（包括它引入的標頭）在函式本體中，或在檔案範圍當作值（例如函式指標初始值）使用 `exit`、`abort`、`_Exit`、
  `quick_exit`、`longjmp`、`raise` 等完整性關卡的終止 / 非區域跳躍名稱，以及 `err`、`errx`、`verr`、`verrx`（CBMC 會默默剪掉這些
  路徑）；任何 `leanc_` 開頭的名稱（不分大小寫）；讀取可編輯原始碼定義、且 inductive 執行保留其初始值的物件（const 或含 const 成分）；
  引入可編輯原始檔；有無 `-DLEANC_VACUITY_CHECK` 的兩份前處理結果除 `LEANC_REACH` 展開外不同；宣告 `__CPROVER_` 名稱。
* verify 執行使用 `--no-self-loops-to-assumptions`：呼叫之後的 `for (;;) {}` 是展開斷言失敗（`INCONCLUSIVE`），不是被剪掉的路徑。
* 列出的 `LEANC_ASSUME` / `__VERIFIER_assume` 逐條寫進範圍，但只是涵蓋狀態的上界：宣告的初始值、最後一個 `LEANC_ASSUME` 之前的
  寫入、第一次 claimed 呼叫之後的寫入、`main()` 沒有直接呼叫 claimed 函式時其中的任何寫入、包住斷言的分支與 helper 函式都會進一步
  縮小涵蓋的狀態，且不會被偵測。結論只和斷言一樣強：請斷言功能性的後置條件，而不只是回傳碼。
* inductive 執行不檢查洩漏（強制檢查類別不含 `memory-leak`）。

## 專案設定 `leanc.yaml`（可選）

```yaml
provider: mock        # 預設 LLM provider：mock | openai
mock_dir: mock
sandbox: auto         # auto | none
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
| `LEANC_HOME` | 批准金鑰與帳本目錄（預設 `$XDG_CONFIG_HOME/leanc`，未設定 `XDG_CONFIG_HOME` 時為 `~/.config/leanc`） |
| `LEANC_CLANG` / `LEANC_CBMC` | 指定工具路徑 |
| `LEANC_GOTO_CC` / `LEANC_GOTO_INSTRUMENT` | 指定 `inductive` 關卡的 goto-cc 與 goto-instrument 路徑（必須與 cbmc 同為 5.95.1） |
| `LEANC_CLANGXX` / `LEANC_FRAMAC` / `LEANC_LEAN` | 指定 clang++、frama-c（需 ≥ 27，含 WP）、lean 的路徑 |
