# 驗證狀態語意

| 狀態 | exit | 意義 | 何時出現 |
|---|---|---|---|
| `VERIFIED_BOUNDED` | 0 | 在**已批准的 harness、其輸入限制、unwind 界限與所啟用的檢查類別**下，CBMC 找不到違反；所有迴圈已完全展開（unwinding assertions 通過）；所有 `LEANC_REACH` 探針可達（非 vacuous）；其他必要關卡也通過 | 所有必要關卡 PASS 且包含 `cbmc` |
| `PROVED` | 0 | 無界證明。**V1 沒有任何後端會產生此狀態**；保留給 V2（Frama-C/WP、Lean 4） | — |
| `COMPILE_ERROR` | 10 | 原始碼無法編譯，或無法與可信測試 driver 連結 | compile 關卡失敗；sanitizer 建置失敗 |
| `STATIC_ANALYSIS_WARNING` | 11 | Clang Static Analyzer 回報問題且政策為 `block` | static_analysis 關卡 FAIL |
| `RUNTIME_TEST_FAILED` | 12 | 可信測試在 ASan/UBSan/LSan 下失敗（sanitizer 報告或結果與預期不符） | sanitizer 關卡 FAIL |
| `COUNTEREXAMPLE_FOUND` | 13 | CBMC 找到具體反例（真實的執行路徑，即使 unwind 不足也成立） | cbmc 關卡 FAIL |
| `UNKNOWN` | 20 | 無法下結論：unwind 不足、harness vacuous / 無探針 / vacuity 檢查停用、必要關卡被略過（例如沒有沙箱）、沒有形式化關卡 | 任一必要關卡 INCONCLUSIVE / SKIPPED |
| `TIMEOUT` | 21 | 工具超時（CBMC 求解、測試執行…） | 任一必要關卡 TIMEOUT |
| `TOOL_ERROR` | 30 | 工具缺失、崩潰、輸出無法解析、記憶體耗盡、後端內部錯誤 | 任一必要關卡 ERROR |
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
* **沒有探針 / 停用 vacuity 檢查**：無法排除 vacuity，因此只能是 `UNKNOWN`。

## 修復與不確定狀態

只有確定性失敗（`COMPILE_ERROR`、`COUNTEREXAMPLE_FOUND`、`RUNTIME_TEST_FAILED`、
`STATIC_ANALYSIS_WARNING`）會進入修復迴圈。`UNKNOWN`、`TIMEOUT`、`TOOL_ERROR`、
`SPEC_UNAPPROVED` 需要人類決策（新規格版本、調整工具、批准），修復迴圈直接回報原狀態。

修復過程中，候選碼只有在以下情況被接受為「改善」：達到成功狀態；或仍為確定性失敗且
排名更好；或狀態相同但失敗數嚴格減少且沒有引入新失敗。讓程式變成不可驗證（`UNKNOWN`/`TIMEOUT`）
永遠不算改善。
