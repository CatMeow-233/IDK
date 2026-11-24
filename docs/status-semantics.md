# 驗證狀態語意

| 狀態 | exit | 意義 | 何時出現 |
|---|---|---|---|
| `VERIFIED_BOUNDED` | 0 | 在**已批准的 harness、其輸入限制、unwind 界限與所啟用的檢查類別**下，CBMC 找不到違反；所有迴圈已完全展開（unwinding assertions 通過）；所有 `LEANC_REACH` 探針可達（非 vacuous）；其他必要關卡也通過 | 所有必要關卡 PASS，包含 `cbmc` 且不含 `wp`（`wp` 也 PASS 時為 `PROVED`） |
| `PROVED` | 0 | **只由 V2 的 `wp` 關卡產生**：Frama-C/WP 證明了已批准（寫在受保護標頭中）的 ACSL 契約的所有證明義務與所有執行期錯誤守衛（`-wp-rte`），smoke test 沒有發現矛盾的前置條件或死碼，WP 套用了每一項有語意影響的 `compile.flags` 以及建置的 `char` 有號性，且其他必要關卡全部通過。對所有滿足前置條件的輸入成立（不受 unwind 界限限制），但只相對於該契約、WP 的 Typed 記憶體模型、x86_64 機器模型、Frama-C libc 規格與所列 prover；除非規格要求 termination，否則是部分正確性。CBMC 與 Lean 都不會產生此狀態 | 必要的 `wp` 關卡 PASS 且其他必要關卡 PASS |
| `COMPILE_ERROR` | 10 | 原始碼無法編譯，或無法與可信測試 driver 連結 | compile 關卡失敗；sanitizer 建置失敗；Lean 向量 driver 建置失敗 |
| `STATIC_ANALYSIS_WARNING` | 11 | Clang Static Analyzer 回報問題且政策為 `block`；或強制的原始碼完整性關卡（`leanc-integrity`）發現驗證敵對構造 | static_analysis / integrity 關卡 FAIL |
| `RUNTIME_TEST_FAILED` | 12 | 可信測試在 ASan/UBSan/LSan 下失敗（sanitizer 報告或結果與預期不符） | sanitizer 關卡 FAIL；lean 關卡 FAIL（向量 driver 的執行失敗：C 實作與 Lean 模型不符、sanitizer 報告、結束碼非 0 或輸出超過上限） |
| `COUNTEREXAMPLE_FOUND` | 13 | CBMC 找到具體反例（真實的執行路徑，即使 unwind 不足也成立）；或 `wp` 關卡中有 prover 把某個證明義務判為 INVALID（不是 CBMC 軌跡，也不一定是具體的執行路徑：該義務可能依賴可編輯的 loop invariant 或 helper 契約） | cbmc 關卡 FAIL；wp 關卡 FAIL |
| `UNKNOWN` | 20 | 無法下結論：unwind 不足、harness vacuous / 無探針 / vacuity 檢查停用、必要關卡被略過（例如沒有沙箱）、沒有形式化關卡（例如 C++ 規格）、WP 目標或 Lean 定理尚未證明、WP 的所有目標都已證明但有 `compile.flags` 項目或建置目標的 `char` 有號性未被 Frama-C 套用（Frama-C 模型下的判決保留在 `wp` 階段的 `details.frama_c_model_verdict`） | 任一必要關卡 INCONCLUSIVE / SKIPPED |
| `TIMEOUT` | 21 | 工具超時（CBMC 求解、測試執行…） | 任一必要關卡 TIMEOUT |
| `TOOL_ERROR` | 30 | 工具缺失、崩潰、輸出無法解析、記憶體耗盡、後端內部錯誤；也包括 provider 失敗：已建立的 provider 在生成時出錯或生成的程式碼被拒絕（`generate`、`run`），`run` 無法為生成建立 provider，或需要修復時無法建立 provider（`repair`、`run`；驗證判定保留在 `initial_status` / `verification.status`，原因在 `messages`） | 任一必要關卡 ERROR；或上述 provider / 生成失敗 |
| `SPEC_UNAPPROVED` | 40 | 沒有可信任的規格：未批准、規格/harness/測試/標頭被改、簽章無效。**報告中的任何結果都不是驗證結果** | spec 關卡 |
| `REPAIR_EXHAUSTED` | 50 | 修復達到最大回合數仍未得到成功狀態；工作區未修改，最佳候選碼保存在 `best-candidate/` | repair / run |

## 彙總優先順序（最差在前）

```
SPEC_UNAPPROVED > REPAIR_EXHAUSTED > COMPILE_ERROR > COUNTEREXAMPLE_FOUND > RUNTIME_TEST_FAILED
  > STATIC_ANALYSIS_WARNING > TOOL_ERROR > TIMEOUT > UNKNOWN > VERIFIED_BOUNDED > PROVED
```

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
  否則為 `TOOL_ERROR`（vacuity 執行看到的會是另一個程式）。
* **沒有探針 / 停用 vacuity 檢查**：無法排除 vacuity，因此只能是 `UNKNOWN`。

## 證明修復（V2）

若 `UNKNOWN` 的**唯一**原因是 `wp` / `lean` 關卡 INCONCLUSIVE（證明義務尚未完成），而其他所有必要關卡都 PASS，
修復迴圈進入「證明修復」：AI 可以加入 loop invariant / loop variant、static helper 的契約、`assert`、Lean 證明，
但不能修改受保護的契約、模型與定理敘述。候選碼只有在仍然只剩證明義務、且未證明的目標嚴格變少（沒有新增）時
才被接受。`wp` 關卡的 `details.compile_flags.not_applied` 非空時不進入證明修復（修改程式碼無法讓該關卡通過），
修復迴圈直接回報原狀態。

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

例外：候選碼讓 `integrity` 由未通過變為通過時，即使隨後解鎖的關卡（sanitizer、cbmc、wp、lean）找到反例、
使狀態排名變差，也算改善；前提是候選碼仍為確定性失敗、沒有上述退化，而且先前已執行的關卡
（`compile`、`static_analysis`）沒有失去 PASS、也沒有新增失敗。
