# inductive_sponge — 歸納驗證（opt-in `inductive` profile，`VERIFIED_INDUCTIVE`）

需求：「實作一個位元組 sponge：`sponge_absorb` 把任意長度的資料吸收進 144 位元組的區塊緩衝區，每填滿一個區塊就
呼叫混合函式並清空緩衝區……對任何長度（不只是小長度）都不得越界存取、不得發生未定義行為，而且吸收必須結束。」

bounded CBMC 只能探索小長度（本例的 bounded harness 只有 6 位元組）；長度問題（例如 `len - i` 被截斷成
`uint32_t`）只在 `len >= 2^32` 時出現。`inductive` 關卡用 CBMC 的 DFCC 迴圈契約（`goto-instrument --dfcc main
--apply-loop-contracts`）對**任意狀態、任意長度**做歸納證明：迴圈只展開一步，invariant、assigns 與 decreases
由工具證明，而不是被假設。

| 檔案 | 角色 | 信任 |
|---|---|---|
| `spec.yaml` | 規格（target `sponge_absorb`；`inductive` 區段；gates `compile, static_analysis, sanitizer, cbmc, inductive`） | 批准後鎖定 |
| `include/sponge.h` | 介面與 `struct sponge`（`block[144]`、`acc[28]`、`fill`、`rounds`） | 受保護 |
| `harness/sponge_harness.c` | bounded CBMC harness：NULL 參數；6 位元組訊息一次吸收與分 1+2+3 段吸收的緩衝內容相同 | 受保護 |
| `harness/sponge_inductive.c` | inductive harness（`main()`）：`LEANC_ASSUME` 狀態不變量、呼叫一次 `sponge_absorb`、之後 `assert` | 受保護 |
| `tests/test_sponge.c` | 可信任測試：完整區塊、分段吸收與一次吸收的摘要相同、`sponge_finalize`、NULL 參數 | 受保護 |
| `src/sponge.c` | 實作（`sponge_mix`、`sponge_new`、`sponge_absorb`、`sponge_finalize`、`sponge_free`） | 可編輯 |
| `inductive/loops.json` | 迴圈契約（`file`、`line`、`invariant`、`assigns`、`decreases`）；只是提案，由工具判定 | 可編輯 |
| `mock/generate/src/sponge.c` | Mock 生成（與 `src/` 相同；非真實 LLM 表現） | — |

```bash
leanc spec approve spec.yaml --by alice --yes
leanc verify spec.yaml                              # => VERIFIED_INDUCTIVE (exit 0)
leanc run spec.yaml --provider mock --regenerate    # => VERIFIED_INDUCTIVE（0 輪修復）
```

`inductive/loops.json`（子句只能用允許的運算式文法，不能用巨集名稱，所以寫 `144` 而不是 `SPONGE_RATE`）：

| 迴圈 | invariant | assigns | decreases |
|---|---|---|---|
| `src/sponge.c:9`（`sponge_mix`） | `k <= 144` | `k`, `__CPROVER_object_upto(s->acc, 28)` | `144 - k` |
| `src/sponge.c:35`（`sponge_absorb`） | `i <= len`, `s->fill < 144` | `i`, `__CPROVER_object_whole(s)` | `len - i` |

## `VERIFIED_INDUCTIVE` 在本例的確切意義

`VERIFIED_INDUCTIVE` 是成功狀態（exit 0），比 `VERIFIED_BOUNDED` 強，但**不是 `PROVED`**（`PROVED` 只來自必要且
通過的 `wp` 關卡）。報告中的範圍敘述（`verification.json` 的 `assumptions`，逐字）：

- `claimed functions: sponge_absorb(); functions that are not claimed have only the bounded CBMC result`
- `contracted loops proved by induction (invariant at entry and after one step, assigns, decreases): src/sponge.c:9 in sponge_mix(), src/sponge.c:35 in sponge_absorb()`
- `harness assumptions of harness/sponge_inductive.c (the LEANC_ASSUMEs only, an upper bound of the covered states; executions outside them are not covered): harness/sponge_inductive.c:11: s != ((void *)0); harness/sponge_inductive.c:12: s->fill < 144; harness/sponge_inductive.c:14: len <= ((size_t)1 << 48); harness/sponge_inductive.c:16: data != ((void *)0)`
- `known gap (not detected): harness code other than LEANC_ASSUME also narrows the covered states and is not listed (declaration initialisers; writes before the last LEANC_ASSUME; writes after the first claimed call of main(), before a later claimed call included; any write in main() when it calls no claimed function directly; branches around the assertions; helper functions): the listed LEANC_ASSUMEs are an upper bound of the covered states, not the covered set, so the approver must review the whole harness`
- `checked property classes: the mandatory CBMC check classes only (bounds, pointer, div-by-zero, signed-overflow, pointer-overflow, undefined-shift), plus every assertion of the inductive harness and the loop invariant, assigns and decreases clauses of every contracted loop; not checked by this gate (bounded CBMC result only): unsigned-overflow`

**已證明**（CBMC 5.95.1，DFCC 迴圈契約，`MANDATORY_CHECKS`）：

- protected inductive harness `main()` **照寫**的每一次執行，每條路徑最多呼叫一次 `sponge_absorb`；呼叫時的狀態
  只受 harness 程式碼限制：`LEANC_ASSUME` 列出 `s != NULL`、`s->fill < 144`、`len <= 2^48`、`data != NULL`
  （涵蓋狀態的上界），`s->rounds`、`s->block`、`s->acc` 與 `data` 的內容任意；本例 harness 在呼叫前沒有其他
  縮小狀態的程式碼，呼叫後的斷言在每條路徑上都會執行。
- `sponge_absorb`（以及它呼叫的 `sponge_mix`）沒有越界、NULL 解參考、除以零、有號溢位、指標溢位、未定義位移。
- 兩個迴圈的 invariant 在進入時成立且被一步保持，迴圈只寫入 assigns 列出的物件，decreases 每步嚴格遞減
  （因此 `sponge_absorb` 對所有 `len <= 2^48` 都會結束）。
- harness 的斷言 `rc == SPONGE_OK` 與 `s->fill < SPONGE_RATE` 在呼叫之後成立。
- 非空洞：vacuity run 中全部 7 個探針都可達（`LEANC_REACH("absorb_returned")`、`sponge_absorb` 入口探針、
  呼叫返回探針、兩個迴圈本體探針；每個迴圈探針在 DFCC 插裝後的程式中被 CBMC 報告兩次，兩次都可達）。

**未證明**（只有 bounded CBMC 結果或可信任測試，或完全不涵蓋）：

- 沒有被 claim 的函式：`sponge_new`、`sponge_finalize`、`sponge_free`。bounded harness 只以 NULL 參數呼叫
  `sponge_finalize`，所以 `sponge_finalize` 的迴圈與摘要計算只有可信任測試涵蓋。
- `unsigned-overflow`（`verification.checks`）只由 bounded 關卡對 6 位元組 harness 檢查；歸納 run 不檢查它
  （任意 `rounds` 下 `s->rounds++` 與混合加總本來就會以模 2^32 環繞，是刻意的）。
- harness 假設之外的狀態：`len > 2^48`、`s->fill >= 144`（只透過 API 修改的 sponge 不會出現，見 PRE1）、
  `data == NULL` 的路徑（由 bounded harness 與測試涵蓋）。
- 摘要的功能正確性、分段吸收的等價性（POST3）：歸納 harness 不斷言這些，只有 bounded harness（6 位元組）與測試涵蓋。
- 模型假設：配置永遠成功；static 物件從任意值開始（本例沒有）；`memcpy` / `memmove` / `memset` 由模型取代：
  檢查來源與目的範圍，並讓目的內容變成任意值。
- 已知缺口 D8（未偵測）：兩個 `memcpy` 都寫入 `struct sponge` 內的 `s->block`；複製模型只檢查整個物件，所以從
  `block` 溢出到同一物件的相鄰欄位 `acc`、`fill`、`rounds` 的複製不會被報告為越界（assigns 是整個 `*s`，所以也不會被 assigns 抓到；
  只可能間接被抓到，例如覆寫 `fill` 而違反 invariant `s->fill < 144`）。複製進字串常值也不會被報告。
- 已知缺口 D20（fail closed）：呼叫含迴圈的 CBMC 函式庫模型（`strlen`、`strcpy`、`strncpy`、`strncat`）會使
  迴圈契約的寫入集檢查失敗，這種程式碼永遠不會通過本關卡（本例沒有使用）。`free` 只有在 `main()` 走得到時，
  `goto-instrument --dfcc` 才能連結它的模型；所以 Lean C 在插裝後的 `main()` 開頭呼叫 `free((void *)0)`，
  只在未被呼叫的函式裡使用 `free` 的程式碼也能執行本關卡。
- harness 結構規則（違反就是 ERROR）：`main()` 裡除了本體最後一個頂層 `return` 以外的任何 `return`；`main()`
  裡的 `goto`；`main()` 從最後一個 `LEANC_ASSUME`（沒有就從開頭）到第一個 claimed 函式呼叫結束之間的任何寫入
  （`=`、複合指定、`++`、`--`，例如 `r->fill = r->fill % 8;`），只有一般宣告 `T x = ...;` 的 `=` 例外；
  harness 翻譯單元（包括它引入的標頭）在函式本體裡使用，或在檔案範圍當作值使用（例如函式指標初始值 `= exit`，
  原型宣告不算）`exit`、`abort`、`_Exit`、`quick_exit`、`longjmp` 等會讓 CBMC 默默剪掉路徑的名稱。verify run
  另外使用 `--no-self-loops-to-assumptions`，所以 `for (;;) {}` 這種自我迴圈不會被 CBMC 變成假設，而是展開斷言
  失敗（`INCONCLUSIVE`）。
- 已知缺口（未偵測）：其他 `LEANC_ASSUME` 以外的 harness 程式碼仍會縮小涵蓋的狀態，而且不會列在範圍敘述裡：
  宣告的初始值、最後一個 `LEANC_ASSUME` 之前的寫入、`main()` 第一個 claimed 呼叫之後的寫入（包括後面另一個
  claimed 呼叫之前的寫入）、`main()` 沒有直接呼叫 claimed 函式時 `main()` 裡的任何寫入、包住斷言的分支（例如只在
  `if (r->fill < 16) { ... }` 裡斷言），以及 helper 函式。列出的 `LEANC_ASSUME` 只是上界，結果只涵蓋 `main()`
  照寫的執行與實際執行到的斷言，所以批准者必須審查整個 harness。

## 有缺陷的版本

`tests/fixtures/inductive/acceptance/sponge/buggy/src/sponge.c` 把條件寫成 `len >= i + SPONGE_RATE`，並把
`len - i` 先截斷成 `uint32_t partial`。bounded CBMC（6 位元組）**通過**；`inductive` 關卡 INCONCLUSIVE，
整體 `UNKNOWN`（exit 20），reason codes：`inductive_step_failed`（`src/sponge.c:35`）、`suspected_non_termination`
（`src/sponge.c:35`，`s->fill != 0` 且 `len - i == 2^32` 時 `partial == 0`，迴圈不前進）、`property_unproved`（`memcpy` 模型：
`memcpy destination region writeable`）。這些失敗的 kind 是 `unproved`，從來不是反例。
見 `tests/test_inductive_acceptance.py`。

修復：在只剩證明關卡未通過的回合，修復代理只能改 `inductive/loops.json`；改程式碼（例如把長度夾小）會在
驗證前被修復政策拒絕（`CONTRACTS_ONLY_ROUND`）。如果修復開始時 `inductive` 關卡不是 PASS，那麼在**任何**回合
（包括同時還有 bounded 反例的回合），只要候選版本的 C 程式碼（`inductive/loops.json` 以外的可編輯檔）和修復
開始時不同、而且 `inductive` 關卡通過，就會在驗證後被拒絕（`INDUCTIVE_CODE_EDIT`）：不接受、不寫回，候選原始碼
留在 `repair/round-<n>/blocked-candidate/` 供人審查。代價：正確的程式碼修正若同時讓 `inductive` 通過，也需要人來套用。

這條規則只限制修復代理。本例的 inductive harness 只斷言 `rc == SPONGE_OK` 與 `s->fill < SPONGE_RATE`，所以
任何作者寫的、把長度截斷到 `2^31 - 1` 的版本（違反「任意長度」與 POST3）一樣會得到 `VERIFIED_INDUCTIVE`；
只有在 harness 斷言功能後置條件（例如所有 `len` 位元組都被吸收）時才能排除這種版本。

成本（CBMC 5.95.1，本機）：`inductive` 關卡約 70 s（verify run 約 35 s，vacuity run 約 35 s）；bounded CBMC 不到 1 s。
`verification.timeout_s: 600` 是每次工具呼叫的上限；逾時是 `TIMEOUT`，從來不是部分通過。
