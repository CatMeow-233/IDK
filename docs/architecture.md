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

## 分層與模組

`leanc` 依職責分成嚴格分層、無循環的套件。每個套件只能 import 下表「允許依賴」所列的套件；同一套件內的模組可以互相
import，但**模組**依賴圖必須無循環。`leanc` 內部的 import 一律寫在模組頂層（不在函式內延遲 import 來繞過循環）；
多個模組共用的東西移到需要它的最低層，不新增向上依賴或轉出（re-export）墊片。根模組 `leanc` 只提供 `__version__`。
這些規則由 `tests/test_architecture.py` 強制檢查（`ALLOWED_DEPENDENCIES` 即下表）。

| 層 | 套件 | 模組 | 允許依賴 |
|---|---|---|---|
| L0 | `leanc/foundation` | `status`、`models`、`errors`、`hashing`、`redact`、`workspace`、`sandbox`、`runner` | （無） |
| L1 | `leanc/specs` | `model`、`loader`、`approval` | foundation |
| L1 | `leanc/lang` | `c_lexer`、`cpp_lexer`、`lean_lexer` | foundation |
| L2 | `leanc/rules` | `definitions`、`acsl`、`c_rules`、`cpp_rules`、`source`、`lean_rules` | foundation、specs、lang |
| L2 | `leanc/toolchain` | `tools`、`cxx`、`framac`、`lean`、`environment`、`context` | foundation、specs |
| L2 | `leanc/reports` | `json_report`、`markdown` | foundation |
| L2 | `leanc/llm` | `base`、`parsing`、`prompts`、`cpp_prompts`、`wp_prompts`、`mock`、`openai_compat`、`factory` | foundation、specs |
| L3 | `leanc/backends` | `clang`、`analyzer`、`sanitizer`、`cbmc`、`counterexample`、`integrity`、`wp`、`lean` | foundation、specs、lang、rules、toolchain |
| L3 | `leanc/repair` | `patch`、`policy` | foundation、specs、lang、rules |
| L4 | `leanc/pipeline` | `controller`、`generation`、`repair_loop`、`doctor`、`config` | foundation、specs、lang、rules、toolchain、reports、llm、backends、repair |
| L5 | `leanc/cli.py`、`leanc/__main__.py` | CLI（argparse）；`__main__` 只依賴 `cli` | 以上全部 |

各層內的分工：

* `foundation`：狀態與彙總規則、pydantic 資料模型、錯誤型別、雜湊、憑證遮蔽、工作區路徑防護、bwrap 沙箱、
  無 shell 的子行程執行（`runner`）。
* `lang`：只做詞法，不含任何規則。`c_lexer.CSource`（行接續、註解與字面值遮蔽、digraph、`#if 0` 區塊、行號對應）、
  `cpp_lexer.CppSource`（數字分隔符、raw string，再交給 C 前端）、`lean_lexer.LeanSource`。
* `rules`：完整性關卡與修復政策共用的純函式信任規則（不呼叫任何工具）。`definitions`（檔案範圍函式定義、
  `is_trivial_body`；C、ACSL、C++ 規則與修復政策共用）→ `acsl`（ACSL 規則）→ `c_rules`（C 原始碼 / 前處理後 /
  物件符號與 section 規則、`ALLOWED_IMPORTS`、名稱集合、標頭事實）→ `cpp_rules`（C++ 規則、demangle、C++ 定義）→
  `source`（依語言分派的 `source_violations`）；`lean_rules`（Lean 證明檔規則與模組配置）。
* `toolchain`：`tools`（`find_tool`、`tool_version` 等基本探測）→ `cxx`（clang++）、`framac`（Frama-C）、
  `lean`（Lean 4 + `leanchecker`）→ `environment`（`Toolchain`、`toolchain()`、`environment_info`）→
  `context`（`BackendContext`、`VerifyOptions`、執行目錄與快照）。
* `backends`：各關卡的執行器。`integrity` 只剩 C 與 C++ 的關卡執行器（`clang -E`、`nm`、`readelf`、`c++filt`），
  規則都在 `rules`；`counterexample` 解析 CBMC 反例。
* `repair`：補丁處理（`patch`）與修復 / 生成政策（`policy.check_candidate` / `check_generated`，以 `lang` 與 `rules` 實作）。
* `pipeline`：驗證控制器、程式碼與規格草案生成、修復迴圈、`leanc doctor`、專案設定。

## 元件對應

| 元件 | 模組 | 說明 |
|---|---|---|
| A. Specification Engine | `leanc/specs/model.py`、`loader.py`、`approval.py`、`leanc/pipeline/generation.py:draft_spec` | 規格 schema（pydantic，`extra="forbid"`）、YAML 重複鍵拒絕、語意雜湊、HMAC 簽章批准、版本規則、AI 草案 |
| B. Code Generation Engine | `leanc/pipeline/generation.py:generate_sources` | 只對已批准規格生成；輸出經 `check_generated` 政策檢查；只寫 editable 檔 |
| C. Compilation Engine | `leanc/backends/clang.py` | `clang -std=c17 -Wall -Wextra -Wpedantic -c`；診斷解析（檔案/行/欄/旗標）；warning 政策 |
| C'. Source Integrity（強制） | `leanc/backends/integrity.py`、`leanc/rules/c_rules.py`、`source.py`、`definitions.py` | 物件檔符號/匯入/section 檢查、前處理層級規則；確保所有工具看到同一份程式、程式不能覆寫 harness/CBMC/libc 符號或終止行程 |
| D. Static Analysis Engine | `leanc/backends/analyzer.py` | `clang --analyze --analyzer-output sarif`；SARIF 解析（含 code flow）；不支援時退回文字格式並記錄 |
| E. Runtime Verification Engine | `leanc/backends/sanitizer/` | ASan/UBSan/LSan 建置可信測試；在 bubblewrap 沙箱中逐案例執行；逾時/輸出/記憶體限制 |
| F. Formal Verification Engine | `leanc/backends/cbmc/` | CBMC 有界模型檢查；強制檢查類別；unwinding assertions；vacuity 探針；可選覆蓋率 |
| G. Counterexample Analyzer | `leanc/backends/counterexample/` | 失敗性質、位置、輸入值、呼叫堆疊、精簡 trace、指紋（fingerprint） |
| H. Repair Agent | `leanc/pipeline/repair_loop.py`、`leanc/repair/patch/`、`policy.py`、`leanc/llm/*` | 提案 → 補丁 → 政策 → 去重 → 快照驗證 → 改善判定 / 還原 |
| I. Verification Controller | `leanc/pipeline/controller.py` | 關卡順序、略過規則、狀態彙總 |
| J. Report Generator | `leanc/reports/json_report.py`、`markdown.py` | JSON 摘要、Markdown 報告（依偵測工具分組） |
| F'. Deductive Verification（V2） | `leanc/backends/wp/`、`leanc/rules/acsl.py`、`leanc/toolchain/framac.py` | Frama-C/WP：受保護標頭中的 ACSL 契約 + `-wp-rte` + smoke test；唯一能產生 `PROVED` 的關卡 |
| F''. Model Proofs（V2，可選） | `leanc/backends/lean/`、`leanc/rules/lean_rules.py`、`leanc/toolchain/lean.py` | Lean 4 kernel + `leanchecker` 檢查 AI 撰寫的模型證明；差分測試向量連結模型與 C |
| C++20（V2） | `leanc/toolchain/cxx.py`、`leanc/rules/cpp_rules/`、`leanc/lang/cpp_lexer.py`、`leanc/llm/cpp_prompts.py` | clang++ 編譯/分析/sanitizer；C++ 完整性規則；沒有形式化關卡 |
| 基礎設施 | `leanc/foundation/runner.py`、`sandbox.py`、`workspace.py`、`redact.py` | 無 shell 執行、rlimit、輸出上限、bwrap 沙箱、路徑穿越/符號連結防護、憑證遮蔽 |

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

* 新後端：在 `leanc/backends/` 實作 `run_xxx(ctx: BackendContext) -> StageResult`，在 `leanc/pipeline/controller.py` 的 `_gate_runner` 註冊，
  並在 `specs/model.py` 的 `GateName` 加入名稱。
* 新 LLM provider：實作 `leanc.llm.base.LLMProvider`，在 `llm/factory.py` 註冊。
* V2 規劃見 [roadmap.md](roadmap.md)。
