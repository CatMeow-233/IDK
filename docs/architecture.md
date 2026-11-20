# Lean C 架構

```
自然語言需求
   │  leanc spec draft（A. Specification Engine，產出「不受信任」草案）
   ▼
候選規格 drafts/<id>/spec.yaml + harness_draft.c + tests_draft.c   ← provenance.origin: ai-draft
   │  人工審查 → leanc spec approve（雜湊鎖定 + HMAC 簽章）
   ▼
已批准規格（spec + harness + 可信測試 + 介面標頭 鎖定）
   │  leanc generate（B. Code Generation Engine，只寫 sources.editable）
   ▼
C 原始碼 ──► I. Verification Controller ───────────────────────────────────────┐
              │ spec → C. 編譯 → 完整性 → D. 靜態分析 → E. Sanitizer → F. CBMC │
              ▼                                                                 │
           VerificationResult（狀態 + 每個關卡 + 診斷 + 反例）                    │
              │ 若為可修復的確定性失敗                                            │
              ▼                                                                 │
           G. Counterexample Analyzer → FailureReport                           │
              ▼                                                                 │
           H. Repair Agent（LLM 提案）→ 政策檢查 → 循環偵測 → 在快照中重新驗證 ──┘
              ▼
           J. Report Generator：run.json / verification.json / report.md / raw/ / logs/
```

## 模組對應

| 元件 | 模組 | 說明 |
|---|---|---|
| A. Specification Engine | `leanc/specs/model.py`、`loader.py`、`approval.py`、`leanc/core/generation.py:draft_spec` | 規格 schema（pydantic，`extra="forbid"`）、YAML 重複鍵拒絕、語意雜湊、HMAC 簽章批准、版本規則、AI 草案 |
| B. Code Generation Engine | `leanc/core/generation.py:generate_sources` | 只對已批准規格生成；輸出經 `check_generated` 政策檢查；只寫 editable 檔 |
| C. Compilation Engine | `leanc/backends/clang.py` | `clang -std=c17 -Wall -Wextra -Wpedantic -c`；診斷解析（檔案/行/欄/旗標）；warning 政策 |
| C'. Source Integrity（強制） | `leanc/backends/integrity.py` | 物件檔符號/匯入/section 檢查、前處理層級規則；確保所有工具看到同一份程式、程式不能覆寫 harness/CBMC/libc 符號或終止行程 |
| D. Static Analysis Engine | `leanc/backends/analyzer.py` | `clang --analyze --analyzer-output sarif`；SARIF 解析（含 code flow）；不支援時退回文字格式並記錄 |
| E. Runtime Verification Engine | `leanc/backends/sanitizer.py` | ASan/UBSan/LSan 建置可信測試；在 bubblewrap 沙箱中逐案例執行；逾時/輸出/記憶體限制 |
| F. Formal Verification Engine | `leanc/backends/cbmc.py` | CBMC 有界模型檢查；強制檢查類別；unwinding assertions；vacuity 探針；可選覆蓋率 |
| G. Counterexample Analyzer | `leanc/repair/counterexample.py` | 失敗性質、位置、輸入值、呼叫堆疊、精簡 trace、指紋（fingerprint） |
| H. Repair Agent | `leanc/repair/loop.py`、`patch.py`、`policy.py`、`leanc/llm/*` | 提案 → 補丁 → 政策 → 去重 → 快照驗證 → 改善判定 / 還原 |
| I. Verification Controller | `leanc/core/controller.py` | 關卡順序、略過規則、狀態彙總 |
| J. Report Generator | `leanc/reports/json_report.py`、`markdown.py` | JSON 摘要、Markdown 報告（依偵測工具分組） |
| F'. Deductive Verification（V2） | `leanc/backends/wp.py`、`acsl_rules.py` | Frama-C/WP：受保護標頭中的 ACSL 契約 + `-wp-rte` + smoke test；唯一能產生 `PROVED` 的關卡 |
| F''. Model Proofs（V2，可選） | `leanc/backends/lean.py`、`leanc/repair/lean_policy.py` | Lean 4 kernel + `leanchecker` 檢查 AI 撰寫的模型證明；差分測試向量連結模型與 C |
| C++20（V2） | `leanc/backends/cxx.py`、`cpp_integrity.py`、`leanc/llm/cpp_prompts.py` | clang++ 編譯/分析/sanitizer；C++ 完整性規則；沒有形式化關卡 |
| 基礎設施 | `leanc/core/runner.py`、`sandbox.py`、`workspace.py`、`redact.py` | 無 shell 執行、rlimit、輸出上限、bwrap 沙箱、路徑穿越/符號連結防護、憑證遮蔽 |

## 資料流與快照

每個指令在 `.leanc/runs/<run_id>/` 下寫 `run.json`、`report.md`、`leanc.log`；每次驗證（初次驗證在
`verify/`，修復回合在 `repair/round-N/verify/`）建立：

```
snapshot/   ← 規格列出的檔案（editable + 受保護檔）的複本；工具只讀這裡
build/      ← 物件檔、SARIF、測試執行檔（唯一可寫的工具輸出目錄）
raw/        ← 原始機器可讀輸出（cbmc-verify.json、cbmc-vacuity.json、*.sarif）
logs/       ← 每次工具呼叫的 stdout/stderr（已遮蔽憑證）
verification.json / verification.md
```

批准檢查讀取的是 **snapshot** 中的受保護檔，因此「被檢查雜湊的位元組」就是「被驗證的位元組」，
避免 time-of-check/time-of-use 差異。修復回合在各自的 `repair/round-N/verify/` 快照中驗證候選碼，
工作區在迴圈期間不被修改；只有達到成功狀態的候選碼才會寫回，原檔備份在 `backup/before-writeback/`
（`generate --force` / `run --regenerate` 覆寫前的原檔在 `backup/before-generate/`；已驗證的候選碼一律另存於
`verified-candidate/`）。

## 關卡規則

1. `spec`：未批准 / 規格或受保護檔被改 / 簽章無效 / 回滾到被取代的版本 → `SPEC_UNAPPROVED`，其餘關卡不執行
   （`--check-unapproved` 可僅供參考地執行，但狀態仍為 `SPEC_UNAPPROVED`）。批准在 CBMC 之前與所有關卡之後
   會在快照上**再檢查一次**；驗證過程中受保護檔被改動 → `SPEC_UNAPPROVED`。
2. `compile` 失敗 → 其餘關卡 `SKIPPED`。`compile` 通過後**一定**執行 `integrity` 關卡（不在 `gates` 清單中、
   不能移除）；它失敗時 sanitizer 關卡 `SKIPPED`（含禁止構造的程式不會被執行），整體為
   `STATIC_ANALYSIS_WARNING`（工具名 `leanc-integrity`）。
3. 其餘關卡互不阻擋（盡量蒐集資訊給修復代理），`--fail-fast` 可改變。
4. 彙總狀態取「最差」者；確定性缺陷優先於不確定結果（見 [status-semantics.md](status-semantics.md)）。
5. `VERIFIED_BOUNDED` 需要所有必要關卡通過 **且** 包含 `cbmc` 關卡。

## 擴充點

* 新後端：實作 `run_xxx(ctx: BackendContext) -> StageResult`，在 `controller._gate_runner` 註冊，
  並在 `specs/model.py` 的 `GateName` 加入名稱。
* 新 LLM provider：實作 `leanc.llm.base.LLMProvider`，在 `llm/factory.py` 註冊。
* V2 規劃見 [roadmap.md](roadmap.md)。
