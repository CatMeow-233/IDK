# 驗證狀態語意

| 狀態 | exit | 意義 | 何時出現 |
|---|---|---|---|
| `VERIFIED_BOUNDED` | 0 | 在**已批准的 harness、其輸入限制、unwind 界限與所啟用的檢查類別**下，CBMC 找不到違反；所有迴圈已完全展開（unwinding assertions 通過）；所有 `LEANC_REACH` 探針可達（非 vacuous）；其他必要關卡也通過 | 所有必要關卡 PASS，包含 `cbmc`，且不含 `wp` 與 `inductive`（`wp` 也 PASS 時為 `PROVED`，`inductive` 也 PASS 時為 `VERIFIED_INDUCTIVE`） |
| `VERIFIED_INDUCTIVE` | 0 | **只由 opt-in 的 `inductive` 關卡產生**：`VERIFIED_BOUNDED` 的所有條件都成立，而且 CBMC 以 DFCC 迴圈契約（`goto-instrument --dfcc main --apply-loop-contracts`）證明：受保護的 inductive harness 中照原樣撰寫的 `main()` 的每一次執行（每條路徑最多呼叫一次 claimed 函式，即 `inductive.functions` 中的函式），都滿足在該路徑上執行到的 harness 斷言，且只檢查強制檢查類別（`MANDATORY_CHECKS`）；契約檔中每個帶契約的迴圈都以歸納法證明（invariant 在進入時成立並被一步保持、只寫入 assigns 列出的物件、decreases 每步嚴格遞減）；vacuity 執行到達所有探針。非 const 的 static 物件從任意值開始，記憶體配置總是成功。範圍見下方「`VERIFIED_INDUCTIVE` 的範圍」；**永遠不會被提升為 `PROVED`** | 所有必要關卡 PASS，包含 `cbmc` 與 `inductive`，且不含 `wp` |
| `PROVED` | 0 | **只由 V2 的 `wp` 關卡產生**：Frama-C/WP 證明了已批准（寫在受保護標頭中）的 ACSL 契約的所有證明義務與所有執行期錯誤守衛（`-wp-rte`），smoke test 沒有發現矛盾的前置條件或死碼，WP 套用了每一項有語意影響的 `compile.flags` 以及建置的 `char` 有號性，且其他必要關卡全部通過。對所有滿足前置條件的輸入成立（不受 unwind 界限限制），但只相對於該契約、WP 的 Typed 記憶體模型、x86_64 機器模型、Frama-C libc 規格與所列 prover；除非規格要求 termination，否則是部分正確性。CBMC（包括 `inductive` 關卡）與 Lean 都不會產生此狀態 | 必要的 `wp` 關卡 PASS 且其他必要關卡 PASS |
| `COMPILE_ERROR` | 10 | 原始碼無法編譯，或無法與可信測試 driver 連結 | compile 關卡失敗；sanitizer 建置失敗；Lean 向量 driver 建置失敗 |
| `STATIC_ANALYSIS_WARNING` | 11 | Clang Static Analyzer 回報問題且政策為 `block`；或強制的原始碼完整性關卡（`leanc-integrity`）發現驗證敵對構造 | static_analysis / integrity 關卡 FAIL |
| `RUNTIME_TEST_FAILED` | 12 | 可信測試在 ASan/UBSan/LSan 下失敗（sanitizer 報告或結果與預期不符） | sanitizer 關卡 FAIL；lean 關卡 FAIL（向量 driver 的執行失敗：C 實作與 Lean 模型不符、sanitizer 報告、結束碼非 0 或輸出超過上限） |
| `COUNTEREXAMPLE_FOUND` | 13 | CBMC 找到具體反例（真實的執行路徑，即使 unwind 不足也成立）；或 `wp` 關卡中有 prover 把某個證明義務判為 INVALID（不是 CBMC 軌跡，也不一定是具體的執行路徑：該義務可能依賴可編輯的 loop invariant 或 helper 契約） | cbmc 關卡 FAIL；wp 關卡 FAIL（`inductive` 關卡從不 FAIL，它的失敗都是 kind `unproved`，不是反例） |
| `UNKNOWN` | 20 | 無法下結論：unwind 不足、harness vacuous / 無探針 / vacuity 檢查停用、必要關卡被略過（例如沒有沙箱）、沒有形式化關卡（例如 C++ 規格）、WP 目標或 Lean 定理尚未證明、WP 的所有目標都已證明但有 `compile.flags` 項目或建置目標的 `char` 有號性未被 Frama-C 套用（Frama-C 模型下的判決保留在 `wp` 階段的 `details.frama_c_model_verdict`）、`inductive` 關卡 INCONCLUSIVE（迴圈沒有契約、契約被拒絕、歸納步驟或性質未證明、疑似不終止、探針不可達、函式未宣告） | 任一必要關卡 INCONCLUSIVE / SKIPPED |
| `TIMEOUT` | 21 | 工具超時（CBMC 求解、測試執行、`inductive` 關卡的任一工具步驟…；逾時永遠不是部分通過） | 任一必要關卡 TIMEOUT |
| `TOOL_ERROR` | 30 | 工具缺失、崩潰、輸出無法解析、記憶體耗盡、後端內部錯誤；`inductive` 關卡 ERROR（`cbmc`、`goto-cc`、`goto-instrument` 不是經過測試的 5.95.1、違反 harness 規則或可信符號規則、CBMC 無法判定的性質）；也包括 provider 失敗：已建立的 provider 在生成時出錯或生成的程式碼被拒絕（`generate`、`run`），`run` 無法為生成建立 provider，或需要修復時無法建立 provider（`repair`、`run`；驗證判定保留在 `initial_status` / `verification.status`，原因在 `messages`） | 任一必要關卡 ERROR；或上述 provider / 生成失敗 |
| `SPEC_UNAPPROVED` | 40 | 沒有可信任的規格：未批准、規格/harness/測試/標頭被改、簽章無效。**報告中的任何結果都不是驗證結果** | spec 關卡 |
| `REPAIR_EXHAUSTED` | 50 | 修復達到最大回合數仍未得到成功狀態；工作區未修改，最佳候選碼保存在 `best-candidate/` | repair / run |

## 彙總優先順序（最差在前）

```
SPEC_UNAPPROVED > REPAIR_EXHAUSTED > COMPILE_ERROR > COUNTEREXAMPLE_FOUND > RUNTIME_TEST_FAILED
  > STATIC_ANALYSIS_WARNING > TOOL_ERROR > TIMEOUT > UNKNOWN > VERIFIED_BOUNDED > VERIFIED_INDUCTIVE > PROVED
```

未通過的必要關卡照常貢獻自己的狀態（INCONCLUSIVE 與 SKIPPED 為 `UNKNOWN`、TIMEOUT 為 `TIMEOUT`、ERROR 為 `TOOL_ERROR`，
FAIL 依關卡而定；`STATUS_OF_FAILED_STAGE` 中 `inductive` 為 `UNKNOWN`，但該關卡從不 FAIL），所以必要的 `inductive` 關卡沒有通過時，結果最好也只是 `UNKNOWN`。
所有必要關卡都 PASS 時：必要的 `wp` 關卡 PASS → `PROVED`（不變）；否則必要的 `inductive` 關卡 PASS 且 `cbmc` PASS →
`VERIFIED_INDUCTIVE`；否則 `cbmc` PASS → `VERIFIED_BOUNDED`（不變）。`VERIFIED_INDUCTIVE` 與 `VERIFIED_BOUNDED` 一樣是成功狀態（exit 0）。

確定性缺陷優先於不確定結果：例如 CBMC 超時但靜態分析找到空指標 → `STATIC_ANALYSIS_WARNING`，
因為已知缺陷是對使用者與修復代理最有用的資訊。

## 「沒有反例」不等於成功

下列情況 CBMC 可能印出 `VERIFICATION SUCCESSFUL`，但 Lean C 回報 `UNKNOWN`：

* **unwind 不足**：`*.unwind.N` 失敗表示有路徑超過界限；在該界限下的「成功」不涵蓋整個輸入空間。
  提高界限需要新版本的已批准規格（修復代理不能改）。
* **vacuous harness**：assume 互相矛盾或程式在呼叫後無法到達（`exit`、無窮迴圈、`abort`），
  所有 assert 都「成立」但沒有意義。Lean C 以 `-DLEANC_VACUITY_CHECK` 重跑，`LEANC_REACH` 探針
  變成 `__CPROVER_assert(0, ...)`；探針「成立」= 不可達 = vacuous。
  每個可編輯原始檔與 harness 在有無 `-DLEANC_VACUITY_CHECK` 時的前處理結果，除探針展開外必須相同，
  否則為 `TOOL_ERROR`（vacuity 執行看到的會是另一個程式）。前處理在任何 CBMC 執行之前各做一次
  （`SOURCE_DATE_EPOCH=0`），verify、coverage 與 vacuity 執行只讀這些寫入 `build/cbmc/` 的 `.i` 文字。
  vacuity 文字中的每個探針位置都要在同一檔案、行與標籤上被 CBMC 回報，否則為 vacuous（`UNKNOWN`）；
  宣告 `__CPROVER_` 名稱（例如名為 `__CPROVER_assert` 的參數）的程式為 `TOOL_ERROR`。
* **沒有探針 / 停用 vacuity 檢查**：無法排除 vacuity，因此只能是 `UNKNOWN`。
* **inductive 關卡**：只有在每個契約都對應恰好一個 CBMC 迴圈且五個迴圈性質類別（`loop_invariant_base`、`loop_invariant_step`、
  `loop_decreases`、`loop_assigns`、`loop_step_unwinding`）都成立、所有性質都成立、而且 vacuity 執行到達每個 `LEANC_REACH`、
  每個 claimed 函式入口的 `leanc_function:<f>`、每個帶契約迴圈本體的 `leanc_loop:<file>:<line>` 與每次 claimed 呼叫之後的
  `leanc_return:<f>` 探針時才 PASS。DFCC 之後仍沒有契約的迴圈（harness 與 CBMC 函式庫之外）是 `loop_without_contract`，
  CBMC 不會執行（展開它只會得到有界的結果）。未成立的性質從任意狀態出發，可能在真實執行中無法到達，所以是 kind `unproved`、
  原因代碼 `inductive_step_failed` / `suspected_non_termination` / `unwind_insufficient` / `unmodelled_callee` / `property_unproved`，
  永遠不是反例。

## `VERIFIED_INDUCTIVE` 的範圍

`verification.json` 的 `assumptions` 逐條列出範圍（`inductive` 階段的 `details.assumptions` 與 `details.scope`），報告與
`leanc --help` 的說明也陳述同一個範圍：

* **已證明**：受保護的 inductive harness 中照原樣撰寫的 `main()` 的每一次執行，每條路徑最多一次 claimed 呼叫，都滿足在該路徑上
  執行到的 harness 斷言，且沒有強制檢查類別（`bounds`、`pointer`、`div-by-zero`、`signed-overflow`、`pointer-overflow`、
  `undefined-shift`）的違反；列出的每個帶契約迴圈（`file:line`）以歸納法證明；claimed 函式與 harness 假設（`LEANC_ASSUME`
  與 `__VERIFIER_assume`）逐條列出。
* **harness 的強度**：列出的 `LEANC_ASSUME` 只是涵蓋狀態的上界。宣告的初始值、最後一個 `LEANC_ASSUME` 之前的寫入、
  `main()` 第一次 claimed 呼叫之後的寫入（包括下一次 claimed 呼叫之前的寫入）、`main()` 沒有直接呼叫 claimed 函式時其中的
  任何寫入、包住斷言的分支與 helper 函式都會進一步縮小涵蓋的狀態，且不回報（未偵測的缺口）。結論只和 harness 斷言一樣強，
  批准者必須審查整個 harness。
* **未證明**（只有有界 CBMC 結果）：沒有被 claim 的函式、harness 照原樣到不了的狀態、`verification.checks` 與
  `contract.safety` 中額外的檢查類別（例如 `unsigned-overflow`）。
* **模型假設**：`memcpy`、`memmove`、`memset` 由模型取代（檢查範圍後讓目的內容變成任意值）；非 const 的 static 物件從任意值
  開始；記憶體配置永不失敗（`verification.malloc_may_fail: true` 與本關卡一起使用會被拒絕）。
* **已知缺口（D8，未偵測）**：經由指標、從成員陣列溢出到同一物件相鄰欄位或填充位元組的存取（`p->a[i]`、指標運算、
  `q = p->a; q[i]`、複製模型），以及複製到字串常值，不會被回報；有界的 `cbmc` 關卡也一樣。
* **已知缺口（D20，fail closed）**：呼叫含迴圈的 CBMC 函式庫模型（`strlen`、`strcpy`、`strncpy`、`strncat`）會使迴圈契約的
  寫入集檢查失敗，這種程式碼永遠無法通過本關卡（`INCONCLUSIVE`）。
* **不是 `PROVED`**：`PROVED` 只來自必要且通過的 `wp` 關卡。

## 證明修復（V2 與 inductive）

若 `UNKNOWN` 的**唯一**原因是 `wp` / `lean` / `inductive` 關卡 INCONCLUSIVE（證明義務尚未完成），而其他所有必要關卡都 PASS，
修復迴圈進入「證明修復」：AI 可以加入 loop invariant / loop variant、static helper 的契約、`assert`、Lean 證明，
但不能修改受保護的契約、模型與定理敘述。候選碼只有在仍然只剩證明義務、且未證明的目標嚴格變少（沒有新增）時
才被接受。`wp` 關卡的 `details.compile_flags.not_applied` 非空時不進入證明修復（修改程式碼無法讓該關卡通過），
修復迴圈直接回報原狀態。

`inductive` 關卡也是證明關卡。只剩證明關卡未通過、且其中包含 `inductive` 的回合只能修改 `inductive.contracts`
（迴圈契約檔）：改動其他任何檔案，包括 C 原始碼，在驗證前就被拒絕（`CONTRACTS_ONLY_ROUND`），否則模型可以用「把長度夾小」
這類修改換取歸納證明。另外，`inductive` 關卡在修復開始時不是 PASS 時，**任何**回合中 C 程式碼（`inductive.contracts` 以外的
可編輯檔）與修復開始時不同、而且通過 `inductive` 關卡的候選，在驗證後被拒絕（`INDUCTIVE_CODE_EDIT`）：不接受、不寫回，原始碼
保存在 `repair/round-<n>/blocked-candidate/` 供人審查；代價是正確的程式碼修正也要由人套用。判定 inductive 證明修復的進展時，
`inductive` 階段的原因代碼（數字以 `#` 取代、不含行號）與失敗一起比較；修復 prompt 附上失敗的迴圈類別與位置、被拒絕的子句與原因。
這些規則只限制修復代理：人寫的、在大尺寸下放棄正確性的程式碼，在弱 harness 下仍可能得到 `VERIFIED_INDUCTIVE`。

## 原因代碼（`reason_codes`）

`VerificationResult.reason_codes`（`verification.json`、`--json` 的 `verification.reason_codes`、Markdown 報告的「Reason codes」）
在成功狀態下為空。否則由未通過的必要關卡導出（`SPEC_UNAPPROVED` 時只看 spec 階段；沒有未通過的必要關卡時看 `cbmc` 階段），每項為
`{code, stage, location, detail}`。有具體代碼的 INCONCLUSIVE 關卡只列具體代碼；其他結果先列通用代碼：FAIL → `gate_failed`、
SKIPPED → `gate_skipped`、TIMEOUT → `gate_timeout`、ERROR → `tool_error`、沒有具體代碼的 INCONCLUSIVE → `gate_inconclusive`。
具體代碼：`unwind_insufficient`、`vacuous_harness`、`vacuity_unchecked`、`unmodelled_callee`、`undeclared_function`、
`property_unproved`、`frama_c_model_only_proof`（WP 在 Frama-C 模型下全部證明，但有未套用的 `compile.flags` 項目），以及只由
`inductive` 關卡產生的 `inductive_step_failed`、`suspected_non_termination`、`contract_rejected`、`loop_without_contract`；
`no_formal_gate` 表示 `UNKNOWN` 的規格既沒有 `cbmc` 關卡也沒有必要的 `wp` 關卡。`inductive` 關卡自己在 `details.reason_codes`
提供代碼，其他關卡的代碼由既有的階段資料導出。

## 沒有形式化關卡的規格（C++20）

C++ 規格不能使用 cbmc / wp；可用 compile / static_analysis / sanitizer / lean（+ 強制 integrity）關卡，最佳結果是誠實的 `UNKNOWN`。
修復迴圈把「所有必要關卡通過」當作目標並寫回程式碼，但狀態維持 `UNKNOWN`，訊息明確說明「NOT formally verified」。

## 修復與不確定狀態

只有確定性失敗（`COMPILE_ERROR`、`COUNTEREXAMPLE_FOUND`、`RUNTIME_TEST_FAILED`、
`STATIC_ANALYSIS_WARNING`）與上述證明修復的 `UNKNOWN` 會進入修復迴圈。其他 `UNKNOWN`、`TIMEOUT`、`TOOL_ERROR`、
`SPEC_UNAPPROVED` 需要人類決策（新規格版本、調整工具、批准），修復迴圈直接回報原狀態。

修復過程中，候選碼只有在以下情況被接受為「改善」：達到成功狀態；或所有必要關卡通過（沒有形式化關卡的規格，
狀態仍為 `UNKNOWN`）；或證明修復中符合上述規則；或仍為確定性失敗且
排名更好；或狀態相同但失敗數嚴格減少且沒有引入新失敗（以不含行號的指紋做多重集合比較，
所以只是讓剩下的缺陷換了行號不算「引入」）。此外，任何先前得到確定判決（PASS/FAIL）的必要關卡
在候選碼下變成 INCONCLUSIVE / TIMEOUT / ERROR / SKIPPED，或新增 unwind / vacuity 失敗，候選碼一律被還原：
讓程式變成不可驗證永遠不算改善。

例外：候選碼讓 `integrity` 由未通過變為通過時，即使隨後解鎖的關卡（sanitizer、cbmc、inductive、wp、lean）找到反例、
使狀態排名變差，也算改善；前提是候選碼仍為確定性失敗、沒有上述退化，而且先前已執行的關卡
（`compile`、`static_analysis`）沒有失去 PASS、也沒有新增失敗。
