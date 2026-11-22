# Lean C

[English](README.md) · [简体中文](README.zh-Hans.md) · **繁體中文**

規格優先、由獨立驗證器裁決的 AI C17 程式碼生成（V2 另支援 C++20、Frama-C/WP、Lean 4）。研究原型。

程式碼由 AI 撰寫，但正確與否由獨立工具判斷：Clang、Clang Static Analyzer、ASan/UBSan/LSan、CBMC，以及可選的 Frama-C/WP 與 Lean 4。它們依據的是經人工批准並雜湊鎖定的規格。

```
需求 -> 規格草案（不受信任）-> 人工批准（雜湊鎖定、HMAC 簽章）
  -> 程式碼生成 -> 編譯 -> 完整性關卡 -> 靜態分析 -> sanitizer 測試
  -> CBMC ->（V2）Frama-C/WP ->（V2，可選）Lean 4
  -> 反例 -> AI 修復（政策檢查，最多 N 回合）-> 重新驗證 -> JSON / Markdown 報告
```

目錄：[安裝](#安裝) · [快速開始](#快速開始) · [概念](#概念) · [指令](#指令) · [狀態與結束代碼](#狀態與結束代碼) · [關卡規則](#關卡規則) · [機器介面（AI agent / CI）](#機器介面ai-agent--ci) · [LLM provider](#llm-provider) · [設定](#設定) · [V2 工具](#v2-可選工具) · [範例](#範例) · [開發 Lean C](#開發-lean-c) · [限制](#限制)

---

## 安裝

需要 Linux 或 WSL2（以下指令以 Ubuntu 24.04 為例），以及 Python 3.11 以上。

```bash
sudo apt-get install -y clang-18 libclang-rt-18-dev cbmc gcc bubblewrap
pip install -e ".[dev]"          # 執行相依：pydantic、PyYAML；開發：pytest
leanc doctor                     # 實測 clang、分析器、sanitizers、gcc、CBMC、bwrap 沙箱與網路隔離、LLM 設定
```

`leanc doctor` 會實際執行每個工具，並為每項失敗的檢查給出修復方式。加上 `--json` 可取得機器可讀輸出（`{"ok": bool, "checks": [{name, ok, required, detail, fix}]}`）。若 bubblewrap 沙箱不可用，sanitizer 測試會被略過，整體狀態最多只能是 `UNKNOWN`（見 [安全性](docs/security.md)）。

## 快速開始

以下全部離線執行，不需要 API key。

```bash
leanc init demo                                   # 範本：spec.yaml、include/、harness/、tests/、src/、leanc.yaml
leanc verify demo/spec.yaml                       # SPEC_UNAPPROVED（exit 40）：沒有已批准的規格就不宣稱任何結果
# 由人審查 demo/spec.yaml、demo/harness/、demo/tests/、demo/include/ 之後：
leanc spec approve demo/spec.yaml --by alice      # 互動確認；加 --yes 可略過
leanc verify demo/spec.yaml                       # VERIFIED_BOUNDED（exit 0）
leanc report latest --project-root demo           # Markdown；--format json 輸出 JSON
```

用一個附帶缺陷的範例跑完整流程（生成 → 驗證 → 反例 → 修復 → 再驗證）：

```bash
cp -r examples/div_by_zero /tmp/dz
leanc spec approve /tmp/dz/spec.yaml --by alice --yes
leanc run /tmp/dz/spec.yaml --provider mock --regenerate
#   initial         COUNTEREXAMPLE_FOUND  (den == 0; INT_MIN / -1)
#   repair round 1: COUNTEREXAMPLE_FOUND  accepted (improvement; continuing)
#   repair round 2: VERIFIED_BOUNDED      accepted (verified)
#   STATUS          VERIFIED_BOUNDED
```

`--provider mock` 會重播範例 `mock/` 目錄中的固定 fixture。它是確定性的，並不代表 LLM 的表現；報告中會標示這是 mock 輸出。

## 概念

| 名詞 | 意義 |
|---|---|
| 專案根目錄 | 最近一個含 `leanc.yaml` 的上層目錄，否則為規格檔所在目錄；可用 `--project-root` 覆寫 |
| 規格（`spec.yaml`） | 可信契約：目標簽名、契約、來源檔、編譯/分析/測試/CBMC 設定、必要 `gates`。格式見 [docs/spec-format.md](docs/spec-format.md) |
| 受保護檔 | 規格、`sources.headers`、`verification.harness`、`tests.driver`，以及 V2 的 ACSL 標頭、Lean spec/vectors。生成與修復永遠不會寫入這些檔案 |
| 可編輯檔 | `sources.editable`：生成與修復唯一能寫入的檔案 |
| 批准 | `leanc spec approve` 把規格的語意雜湊與每個受保護檔的 SHA-256 綁進一筆紀錄，並用 `LEANC_HOME`（預設 `~/.config/leanc`）中的金鑰簽章。任何變更都會導致 `SPEC_UNAPPROVED`，直到重新批准為止，而且 `version` 必須遞增。回滾帳本會拒絕已被取代的批准 |
| 規格草案 | `leanc spec draft` 寫入 `drafts/<id>/`，並標記為 `provenance: ai-draft`。在人工審查並批准之前，它永遠不受信任 |
| 關卡（gate） | 一個驗證階段：`compile`、`integrity`（強制；在編譯關卡通過後執行）、`static_analysis`、`sanitizer`、`cbmc`，V2 另有 `wp`、`lean`。必須每個必要關卡都執行且通過，才算成功 |
| 執行（run） | 每次 `generate` / `verify` / `repair` / `run` 都會建立 `.leanc/runs/<run_id>/` |

給所有 agent 的規則：不得修改受保護檔，也不得代替人執行 `spec approve`。批准就是信任邊界。

## 指令

| 指令 | 作用 | 建立 run |
|---|---|---|
| `leanc doctor` | 檢查工具鏈、沙箱與 LLM 設定 | 否 |
| `leanc init [DIR] [--template safe-array-get] [--force]` | 由範本建立專案 | 否 |
| `leanc spec draft --id ID (--requirement TEXT \| --requirement-file F)` | 需求 → `drafts/<id>/` 中不受信任的草案（exit 40） | 否 |
| `leanc spec approve SPEC --by NAME [--note N] [--yes]` | 人工批准（雜湊鎖定 + 簽章） | 否 |
| `leanc spec status SPEC` | 批准狀態、簽章有效性、批准後的變更 | 否 |
| `leanc spec show SPEC` | 正規化規格、雜湊、有效的 CBMC 檢查 | 否 |
| `leanc generate SPEC [--force]` | 為已批准的規格寫入 `sources.editable`（exit 0 表示已生成，**尚未**驗證） | 是 |
| `leanc verify SPEC` | 對目前的原始碼執行各驗證關卡 | 是 |
| `leanc repair SPEC [--max-rounds N] [--no-write]` | 先驗證，再進行反例驅動的修復迴圈 | 是 |
| `leanc run SPEC [--regenerate] [--no-repair] [--max-rounds N] [--no-write]` | 規格檢查 → 生成（若不存在）→ 驗證 → 修復 → 再驗證 → 報告 | 是 |
| `leanc report [RUN\|latest] [--format md\|json]` | 顯示某次執行的報告；以該次執行的狀態作為 exit code；若成功的執行之後檔案被修改，顯示 STALE 並回傳 20 | 否 |

共用選項：

| 選項 | 適用指令 | 作用 |
|---|---|---|
| `--json` | `report` 以外全部 | stdout 只輸出單一 JSON 物件，日誌寫到 stderr |
| `-v` | `report` 以外全部 | 詳細日誌寫到 stderr |
| `--project-root DIR` | spec、generate、verify、repair、run、report | 覆寫專案根目錄 |
| `--gates a,b` | verify、repair、run | 只執行規格關卡的子集（結果最多 `UNKNOWN`） |
| `--fail-fast` | verify、repair、run | 第一個未通過的關卡之後就停止 |
| `--timeout-scale X` | verify、repair、run | 所有工具逾時乘以 X |
| `--no-sandbox` | verify、repair、run | 不使用 bwrap；動態測試會 `SKIPPED`，結果最多 `UNKNOWN` |
| `--allow-unsandboxed` | verify、repair、run | 危險：在沒有沙箱的情況下執行不可信的測試程式（只有這個旗標能這樣做，`leanc.yaml` 不行） |
| `--check-unapproved` | verify、repair、run | 對未批准的規格執行關卡，結果僅供參考；狀態仍是 `SPEC_UNAPPROVED` |
| `--provider mock\|openai`、`--mock-dir D`、`--llm-timeout S` | spec draft、generate、repair、run | 選擇 LLM |

## 狀態與結束代碼

| exit | 狀態 | 意義 |
|---|---|---|
| 0 | `PROVED` | 必要的 `wp` 關卡證明了已批准的 ACSL 契約與所有 RTE 守衛（smoke test 無異常）。這是 `PROVED` 唯一的來源 |
| 0 | `VERIFIED_BOUNDED` | 在已批准的 harness、輸入界限與 unwind 界限內沒有違反；所有迴圈都已完全展開；harness 不是 vacuous。**永遠不會升級為 `PROVED`** |
| 10 | `COMPILE_ERROR` | 編譯錯誤（或在 `warnings_policy: block` 下出現警告） |
| 11 | `STATIC_ANALYSIS_WARNING` | 在 `policy: block` 下有分析器發現，或違反完整性關卡 |
| 12 | `RUNTIME_TEST_FAILED` | sanitizer 回報、exit code 或輸出錯誤，或 Lean 測試向量不符 |
| 13 | `COUNTEREXAMPLE_FOUND` | CBMC 找到具體反例（真實的執行路徑，即使 unwind 不足也成立） |
| 20 | `UNKNOWN` | 無法下結論：unwind 不足、harness vacuous、關卡被略過、只跑關卡子集、沒有形式化關卡（C++）、目標未證明 |
| 21 | `TIMEOUT` | 必要工具逾時 |
| 30 | `TOOL_ERROR` | 工具崩潰或輸出無法解析 |
| 40 | `SPEC_UNAPPROVED` | 目前的規格與受保護檔沒有有效批准 |
| 50 | `REPAIR_EXHAUSTED` | 修復迴圈結束但沒有成功；工作區已還原 |
| 2 | — | 使用方式或輸入錯誤 |
| 70 | — | 內部錯誤（traceback 寫到 stderr） |

「沒找到反例」不等於成功。逾時、工具錯誤、無法解析的結果、被略過的關卡，都不算成功。彙總規則見 [docs/status-semantics.md](docs/status-semantics.md)。

## 關卡規則

這些規則堵住了「關卡通過、但程式碼其實沒被真正檢查」的漏洞；仍未堵住的列在[限制](#限制)中。它們都不會把失敗變成成功。

| 規則 | 行為 |
|---|---|
| 完整性未通過 | `sanitizer`、`cbmc`、`wp`、`lean` 只在完整性關卡通過後執行，否則為 `SKIPPED`（報告原文：「never executed, model-checked or proved」）；`gates` 不含 `compile` 時同樣如此，因為完整性關卡在編譯關卡通過後才執行。`static_analysis` 照常執行。狀態取自完整性關卡：`FAIL` 得 `STATIC_ANALYSIS_WARNING`，`ERROR` 得 `TOOL_ERROR`，`TIMEOUT` 得 `TIMEOUT`；完整性關卡沒有執行時，狀態為 `UNKNOWN`。修復迴圈中，讓完整性通過的候選算作進展，即使隨後解鎖的關卡找到反例，前提是先前已執行的關卡（`compile`、`static_analysis`）沒有失去通過、也沒有新增失敗 |
| 堆疊框大小 | 完整性關卡以 `-fstack-usage` 編譯每個可編輯檔案（先以 `-include` 加入一個錨點函式，再加去掉警告控制後的 `compile.flags`，最後是 `-O0 -fno-lto -fembed-bitcode=off`），讀取 clang 寫出的堆疊框大小：超過 1 MiB 的堆疊框為 `STACK_FRAME_TOO_LARGE`，clang 標為 `dynamic` 的堆疊框（執行期才配置的堆疊：VLA、`alloca`、`_alloca`）為 `UNBOUNDED_STACK`。錨點讓 clang 為每個產生程式碼的檔案寫出堆疊用量檔；沒有寫出時（例如 `-emit-llvm`）或編譯的結束碼不是 0，完整性關卡為 `ERROR`。VLA / `alloca` 檢查（`UNBOUNDED_STACK`）同樣帶上 `compile.flags` 編譯，並計入 clang 回報的每一則診斷；第二次執行加上 `-Wsystem-headers`，計入專案自身檔案中的診斷，因此 `-isystem` 之類的選項無法隱藏受保護標頭中的 VLA，而 libstdc++ 等函式庫標頭不計 |
| 三字元序列 | 可編輯的 C 程式碼中任何位置（包括註解與字串常值）出現三字元序列（`??/`、`??=` 等）即為 `TRIGRAPH`：clang 在 ISO C 模式（`-std=c17`）下會替換三字元序列，CBMC 與 Frama-C 的前處理則不會，所以註解結尾的 `??/` 只會對其中一方隱藏下一行。同理，`//*` 為 `TOOL_DIVERGENCE`：`compile.flags` 帶 `-std=c89` 或 `-ansi` 時，clang 把它讀成除號加區塊註解，CBMC 的 gnu89 前處理則讀成行註解 |
| 預先定義的巨集 | 僅適用於含 `cbmc` 或 `wp` 的規格。把 clang 在編譯關卡選項（`-std`、`compile.flags`、`compile.defines`）下預先定義的巨集，與 gcc 在各驗證器自身前處理方式下預先定義的巨集比較。CBMC：`-D__CPROVER__ -m64 -std=gnuNN`、`-funsigned-char`、它收到的 `-D` 定義，然後是它的 x86-64 定義（`__LP64__`、`__x86_64__` 等），順序與 CBMC 呼叫 gcc 時相同。Frama-C：`-undef -nostdinc` 加 machdep 的內建巨集（`frama-c -print-machdep-builtin-macros`，其中定義了 `unix` 與 `linux`）、`-D__FRAMAC__ -std=c11` 與 `compile.defines`。可編輯程式碼中用到取值不同的巨集即為 `TOOL_DIVERGENCE`。需要 `gcc`，`wp` 還需要 `frama-c`；缺少時完整性關卡為 `ERROR` |
| `compile.flags` | 每一項都是一個選項，值直接接在選項後面（`-Iinclude`、`-DNAME=1`、`-std=c17`、`-includepre.h`）。規格驗證拒絕輸入檔、`-` 與 `--`。完整性關卡會詢問 clang（`clang -### <項>`）某一項是否把下一項當作自己的值（`-I include`、`-iprefix -funsigned-char`）；這樣的項得 `TOOL_ERROR`，因為 CBMC 轉換與堆疊檢查把每一項都當作獨立選項讀取。另外拒絕：`-fsanitize*` / `-fno-sanitize*`（sanitizer 由 `tests.sanitizers` 決定），以及把參數交給內部工具或替換工具鏈的選項：`-X...`（`-Xclang`、`-Xanalyzer`、`-Xlinker` 等）、`--for-linker`、`-mllvm`、`-Wp,` / `-Wa,` / `-Wl,`、`--analyze...`、`@file`、`--config`、`-fplugin` / `-fpass-plugin`、`-ccc-*`、`-B` / `--prefix`、`--ld-path` / `-fuse-ld=<path>`、`-resource-dir` / `--resource-dir`、`--sysroot` / `-isysroot`、`--gcc-toolchain` / `--gcc-install-dir`、`-nostdlib` / `--no-standard-libraries` / `-nodefaultlibs` / `-nostartfiles`、`--driver-mode`。改變前處理器輸出或在產生程式碼之前就停止的選項也會被拒絕，因為完整性關卡以 `compile.flags` 讀取前處理後的原始碼：`-E`、`-P` / `--no-line-commands`、`-C` / `-CC`、`-d*`（`-dM`、`-dD` 等）、`-M` / `-MM`、`-fdirectives-only`、`-frewrite-includes`、`-fuse-line-directives`、`-fkeep-system-includes`、`-fminimize-whitespace`、`-traditional-cpp`、`-fsyntax-only`、`-fdriver-only`；前處理輸出中沒有該檔案的 linemarker 時為 `ERROR`。`-Wwrite-strings` 會被拒絕：clang 把它轉成 `-fconst-strings`，讓字串常值的型別變成 `const char[N]`，而沒有任何驗證器套用這一點。靜態分析收到其餘參數。CBMC 會收到 `-D`、`-I`、`-std=` / `--std=` 與 `-funsigned-char`（有號 `char` 是 CBMC 在 x86 上的預設），GCC 的長拼法（`--define-macro=`、`--undefine-macro=`、`--include-directory=`、`--unsigned-char`、`--signed-char`）也一樣，但絕不會收到 `NDEBUG`。`-U` 只能取消它之前的 `-D`（函式型巨集也一樣）；其他 `-U` 會被列為未套用。警告與 `-g` 參數沒有語意影響，但 `-Wdeprecated` / `-Wno-deprecated` 除外：它們在 C++ 中決定 `__DEPRECATED`，因此會保留給堆疊檢查。其餘參數（例如 `-O2 -ffast-math -mavx2`）列在假設「compile.flags not applied by CBMC」中；WP 不套用任何 `compile.flags`，並把它們列在「compile.flags not applied by Frama-C」中。判定不變 |
| 無函式本體的函式（CBMC） | 允許：`nondet_*`、`__CPROVER*`、`__builtin_prefetch`、`__builtin_expect`。其他無函式本體的被呼叫函式會使 `cbmc` 無法通過：`<immintrin.h>` 背後的 SIMD builtin，以及完整性關卡允許、但 CBMC 沒有模型的 `memchr`、`strnlen`、`strstr`、`fputc`、`putc`。路徑用到這類函式回傳值的違規是 `unmodelled-callee` 診斷，不是反例；其他違規仍是反例。沒有反例時關卡為 `INCONCLUSIVE`（狀態 `UNKNOWN`）：把呼叫改寫成一般 C 才能得到判定 |
| CBMC 內部錯誤 | 不變式違規報告為 `TOOL_ERROR`（「CBMC internal error」），並附上 File/Condition/Reason 摘錄，而不是記憶體不足提示 |
| 契約安全檢查 | `contract.safety` 中的檢查會加入 CBMC 的性質檢查；規格雜湊不變 |
| sanitizer 建置 | 測試、所有權追蹤和 Lean 向量的建置都在 `compile.flags` 之後加上 `-fno-builtin -fno-optimize-sibling-calls`，這樣 clang `-O1` 無法刪除未使用的配置（`malloc`、`strdup`、`aligned_alloc`、C++ `new`），也無法刪除包裝函式的堆疊框 |
| 記憶體檢查的侷限 | `limitations` 列出洩漏檢查看不到的部分；只有關卡得出判定（`PASS` 或 `FAIL`）的檢查才算數。LeakSanitizer 只回報不可達的區塊。沒有 CBMC `memory-leak` 判定時，只在測試路徑上檢查洩漏。CBMC 不檢查對齊、嚴格別名、`restrict`，也不檢查同一配置內子物件之間的溢位 |

## 機器介面（AI agent / CI）

使用 `--json`，並依 exit code 或 `status` 分支。`leanc verify SPEC --json` 的輸出如下：

```json
{
  "command": "verify", "status": "COUNTEREXAMPLE_FOUND", "exit_code": 13,
  "run_id": "verify-20261121T034210Z-7c8afd", "run_dir": ".../.leanc/runs/verify-...",
  "provider": null, "report": ".../report.md", "messages": ["..."],
  "verification": {
    "status": "...", "success": false, "spec_id": "...", "spec_version": 1, "spec_hash": "...",
    "approval": {"...": "..."},
    "stages": [{"stage": "cbmc", "outcome": "FAIL", "status": "...", "required": true, "invoked": true,
                "duration_s": 1.2, "summary": "...", "tools": [], "failures": []}],
    "detected_by": ["asan", "cbmc"], "reasons": ["..."], "assumptions": ["..."], "limitations": ["..."],
    "failures": [{
      "id": "sanitizer:index_equals_count:asan:heap-buffer-overflow", "tool": "asan", "stage": "sanitizer",
      "kind": "...", "property_id": "...", "location": {"file": "src/array_lookup.c", "line": 9, "column": 16, "function": "array_lookup"},
      "inputs": [{"name": "argv", "value": "index_equals_count", "type": null}],
      "description": "...", "raw_ref": "raw/...", "ownership_trace": null
    }]
  }
}
```

執行目錄結構：

```
.leanc/runs/<run_id>/
  run.json  report.md  leanc.log
  verify/                         （修復回合：repair/round-N/verify/）
    verification.json  verification.md
    snapshot/   實際被驗證的原始碼
    raw/        工具原始輸出：cbmc-verify.json、compile/、integrity/、static_analysis/（SARIF）、sanitizer/、wp/、lean/
    logs/       每次工具呼叫的 stdout / stderr，依執行順序編號
    build/
```

C 專案出現 LeakSanitizer 失敗時，`ownership_trace` 保存對該洩漏測試案例做插樁重放得到的證據。它不參與判定。C++ 和多執行緒的測試程式不追蹤。內容如下：

- `allocation`：配置點。
- `released_at`：同一配置點的記憶體在其他路徑上被釋放的位置。
- `loss` 與 `function`：最後一個參考消失時間的上界。只有在某次檢查中被列為 Direct 洩漏才算證據；LeakSanitizer 的掃描是保守的，掃描中仍找到看似指標的值並不能證明任何事。`loss` 是以下之一：
  - `lost-during-call`：該記憶體在呼叫 `function` 期間配置，且在 `function` 返回前已不可達，所以是在 `function` 或其被呼叫者中遺失的。這是唯一的強結論。
  - `unreachable-by-return`：`function` 不在配置時的呼叫堆疊上，到它即將返回時已沒有任何參考。這不說明是在哪裡遺失的。
  - `unreachable-by-next-event`：在 `function` 返回後的第一個被追蹤事件之前，於 `function` 或其呼叫者中遺失。
  - `not-localised`：程式結束前沒有任何檢查發現它不可達。仍會回報 `released_at`。
- `returned_to`：`function` 返回到的呼叫點；被 inline 的函式不提供。

修復 prompt 會以明確標籤顯示同樣的證據。它不會指出執行的是哪一行 `return`。

agent 修正程式碼有兩種方式：

1. **外部 agent 迴圈。** 執行 `leanc verify SPEC --json`，讀取 `verification.failures`（位置、輸入、性質）與 `raw_ref`。只修改 `sources.editable` 列出的檔案，然後再驗證。遇到 exit 0，或 40（需要人處理）、20/21/30（不是程式碼缺陷，請看 `reasons`）時停止。
2. **內建迴圈。** 執行 `leanc run SPEC --provider openai --json`（或 `repair`）。Lean C 會呼叫模型、對每個補丁做政策檢查，並且只把驗證通過的候選寫回（`--no-write` 讓工作區保持不變）。

修復政策會拒絕以下補丁：寫入 `sources.editable` 以外的檔案、加入 `__CPROVER_*` / `LEANC_*` / `__builtin_assume`、刪除 `assert`、抑制警告或 sanitizer、`#include` 逃逸、`system`/`exec`/`fork`/`socket`、inline asm、把目標函式掏空，以及過大的補丁。重複的補丁會被判定為循環。結果變成 `UNKNOWN`/`TIMEOUT` 不算改善。

## LLM provider

**mock**（`leanc.yaml` 沒有設定 provider 時的預設值）。它從 `<project>/mock/` 讀取 fixture（可用 `--mock-dir` 覆寫）：

```
mock/
  spec_draft.yaml、harness_draft.c、tests_draft.c    `spec draft` 回傳的內容
  generate/<可編輯檔路徑>          例如 generate/src/safe_ratio.c
  repair/round-<N>.json            {"root_cause": "...", "rationale": "...", "files": {"src/x.c": "<完整內容>"}}
                                   或以 "patch": "<unified diff>" 取代 "files"；缺少某回合 = 沒有提案
```

**openai**（任何 OpenAI 相容的 chat-completions 端點）：

| 變數 | 意義 |
|---|---|
| `LEANC_API_KEY`（或 `OPENAI_API_KEY`） | API key。日誌、報告與 transcript 中都會遮蔽，也不會傳給子行程。若以 `LEANC_BASE_URL` 指向本機伺服器，可以留空 |
| `LEANC_MODEL` | 模型名稱（必填） |
| `LEANC_BASE_URL` | 預設 `https://api.openai.com/v1` |
| `LEANC_LLM_TIMEOUT` | 請求逾時秒數 |
| `LEANC_LLM_MAX_RETRIES` | 暫時性錯誤的重試次數 |
| `LEANC_LLM_MAX_TOKENS` | 最大生成 token 數（看到 `finish_reason=length` 時請調高） |
| `LEANC_LLM_TEMPERATURE` | 數字或 `default` |
| `LEANC_LLM_JSON_MODE=1` | 送出 `response_format: json_object` |
| `LEANC_LLM_PROXY` | `env`（使用 `HTTPS_PROXY`）或 `none` |

```bash
export LEANC_API_KEY=... LEANC_MODEL=<model>
leanc spec draft --id my-func --requirement "實作一個……" --provider openai   # 不受信任的草案 -> 審查 -> 批准
leanc run path/to/spec.yaml --provider openai --json
```

prompt 位於 `leanc/llm/prompts/`。評估模型時，請自行記錄模型、溫度與 prompt 版本。

## 設定

專案根目錄可放一個可選的 `leanc.yaml`：

```yaml
provider: mock        # 預設 provider：mock | openai
mock_dir: mock
sandbox: auto         # auto | none
```

專案設定不能削弱檢查、界限、關卡、批准或隔離。若設定了 `allow_unsandboxed`，該值會被忽略並發出警告。

| 變數 | 意義 |
|---|---|
| `LEANC_HOME` | 批准金鑰與帳本目錄（預設 `~/.config/leanc`）；請放在工作區之外 |
| `LEANC_CLANG`、`LEANC_CLANGXX`、`LEANC_CBMC` | 工具路徑 |
| `LEANC_FRAMAC` | `frama-c`（>= 27，含 WP）；未設定時搜尋 `PATH` 與 `/opt/opam/*/bin` |
| `LEANC_LEAN` | `lean`（含 `leanchecker` 的 toolchain）；未設定時搜尋 `PATH` 與 `/opt/lean/*/bin` |

## V2 可選工具

| 功能 | 安裝 | 規格 | 對狀態的影響 |
|---|---|---|---|
| Frama-C/WP + ACSL | `sudo apt-get install -y opam z3 cvc5 && opam init -y && opam install -y frama-c alt-ergo`（Ubuntu 的 `frama-c-base` 不含 WP） | ACSL 契約寫在受保護標頭中，關卡 `wp` | `PROVED` 唯一的途徑；證明修復讓 AI 補上 loop invariant |
| Lean 4 | release toolchain 解壓到 `/opt/lean/<版本>`，或使用 elan | 受保護的模型與定理敘述；證明由 AI 撰寫；kernel + `leanchecker`；只接受標準公理；差分測試向量 | 模型層級：未證明 → `UNKNOWN`，向量不符 → `RUNTIME_TEST_FAILED`；永遠不會產生 `PROVED` |
| C++20 | `clang++`（clang-18 內含） | `language: {name: cpp, standard: c++20}` | 沒有可用的 C++ model checker，因此最佳結果是 `UNKNOWN` |

`leanc doctor` 會回報上述每個工具。缺少某個工具時，相關測試會被略過，而不是失敗。

## 範例

每個 `examples/<name>/` 都包含 `spec.yaml`、受保護的 `include/`、`harness/`、`tests/`，附帶的（通常有缺陷的）`src/`，`mock/` fixture，以及 `expected.yaml`。

| 範例 | 附帶的程式碼 | `verify` | `run --provider mock` |
|---|---|---|---|
| `safe_array_get` | 正確 | `VERIFIED_BOUNDED` | `VERIFIED_BOUNDED` |
| `array_oob` | `index <= count` | `COUNTEREXAMPLE_FOUND`（cbmc、asan） | 1 回合 → `VERIFIED_BOUNDED` |
| `div_by_zero` | 未檢查 `den` | `COUNTEREXAMPLE_FOUND`（cbmc、ubsan） | 2 回合 → `VERIFIED_BOUNDED` |
| `int_overflow` | 先加再檢查 | `COUNTEREXAMPLE_FOUND` | 1 回合 → `VERIFIED_BOUNDED` |
| `null_deref` | 檢查了 NULL 但沒有返回 | `COUNTEREXAMPLE_FOUND`（analyzer、cbmc、ubsan） | 1 回合 → `VERIFIED_BOUNDED` |
| `loop_unwind` | 正確，但 unwind 不足 | `UNKNOWN` | `UNKNOWN`（不會被「修復」）；變體 `spec_full_unwind.yaml` → `VERIFIED_BOUNDED` |
| `vacuous_harness` | assume 互相矛盾 | `UNKNOWN`（單用 CBMC 會說 SUCCESSFUL） | `UNKNOWN` |
| `runtime_memory` | 錯誤路徑洩漏 | `RUNTIME_TEST_FAILED`（lsan） | 1 回合 → `VERIFIED_BOUNDED` |
| `ring_buffer` | 兩個 `.c`，正確 | `VERIFIED_BOUNDED` | `VERIFIED_BOUNDED` |
| `cheating_repair` | off-by-one | `COUNTEREXAMPLE_FOUND` | 第 1–4 回合被政策拒絕，第 5 回合 → `VERIFIED_BOUNDED` |
| `repair_exhausted` | `(a+b)/2` 溢位 | `COUNTEREXAMPLE_FOUND` | `REPAIR_EXHAUSTED`，工作區不變 |
| `cbmc_timeout` | 困難的 64 位元等價性 | `TIMEOUT` | `TIMEOUT` |
| `wp_safe_array_get` | ACSL 契約，正確 | `PROVED` | `PROVED` |
| `wp_proof_repair` | 正確，但沒有 loop invariant | `UNKNOWN` | 2 回合證明修復 → `PROVED` |
| `wp_contract_violation` | 違反後置條件 | `COUNTEREXAMPLE_FOUND` | 1 回合 → `PROVED` |
| `lean_clamp` | Lean 定理留為 `sorry` | `UNKNOWN` | 2 回合 → `VERIFIED_BOUNDED` |
| `cpp_ring_buffer` | C++20 class template | `UNKNOWN` | `UNKNOWN` |
| `cpp_bounds_bug` | C++ off-by-one | `RUNTIME_TEST_FAILED`（asan） | 1 回合 → `UNKNOWN` |

全部重現：

```bash
python scripts/run_experiments.py                       # 全部範例；應回報 N/N 符合
python scripts/run_experiments.py --only array_oob div_by_zero
```

腳本會把每個範例複製到暫存專案，並以暫時的 `LEANC_HOME` 批准。接著透過真正的 CLI 執行 `verify` 與 `run --provider mock --regenerate`，再與 `expected.yaml` 比對結果。輸出寫到 `experiments/results/`（本機產生，已列入 gitignore）。詳見 [docs/experiments.md](docs/experiments.md)。

## 開發 Lean C

規則宣告在 [`constraints.toml`](constraints.toml) 中。它的作用相當於 yarn 專案裡的 constraints 檔，是唯一的規則來源。`python -m scripts.constraints` 檢查這些規則，`tests/test_architecture.py` 執行的是同一個引擎。agent 指令寫在 [`CLAUDE.md`](CLAUDE.md) 中，其中的分層區塊由 `constraints.toml` 自動產生。

- 嚴格的單向分層、無循環的模組圖，且只允許頂層 import：
  ```
  foundation < specs, lang, reports < rules, toolchain, llm < backends, repair < pipeline < cli < __main__
  ```
  共用程式碼往下層移。套件的 `__init__` 只能 re-export 自己的子模組。
- `leanc/`、`tests/`、`scripts/` 中不得有註解或 docstring。意圖要用名稱、常數、Enum 與 frozen dataclass 表達。
- 不得有 bare/`Exception`/`BaseException` handler（唯一的例外是 `leanc/cli.py` 的 `main()`，它以 exit 70 結束），也不得吞掉例外。
- 每個 `.py` 檔最多 600 行。

| 套件 | 職責 |
|---|---|
| `foundation` | 狀態、資料模型、錯誤、雜湊、憑證遮蔽、工作區（路徑/符號連結安全）、沙箱、`run_command`（只接受 argv、資源限制） |
| `specs` | 規格 schema、載入、雜湊、批准與帳本 |
| `lang` | C / C++ / Lean lexer |
| `rules` | 關卡與修復政策共用的純函式信任規則（C、C++、ACSL、Lean、前處理器） |
| `toolchain` | 工具探測、`BackendContext` |
| `reports` | JSON / Markdown 輸出 |
| `llm` | provider 介面、mock、OpenAI 相容、prompts、回應解析 |
| `backends` | 各關卡：clang、analyzer、integrity、sanitizer、cbmc、counterexample、wp、lean |
| `repair` | 套用補丁（fuzz 0）、生成與修復政策 |
| `pipeline` | 驗證控制器、生成、修復迴圈、doctor、設定、指令實作 |
| `cli.py` | 只有 argparse 與 `main` |

檢查：

```bash
python -m scripts.constraints                       # 檢查 constraints.toml 的全部規則；有違規時 exit 1；支援 --json
python -m scripts.constraints --fix                 # 只在 AST 不變時刪除註解/docstring；重新產生 CLAUDE.md 的分層區塊
python scripts/affected_tests.py --run              # 修改後：依 import 圖相對於 HEAD 選出受影響的測試
python scripts/affected_tests.py --base main --run  # 整個分支
python -m scripts.merge_gate                        # 建立或合併 PR 之前執行（約 20 分鐘）
```

`python -m scripts.merge_gate` 在即將被合併的那個 HEAD 上強制執行「修正 → 測試 → 合併」。它依以下順序執行，第一個失敗就停止，沒有略過選項：

1. 用明確的 refspec 取得基準分支。要求所有改動都已提交、HEAD 與基準不同，且 HEAD 包含基準分支。
2. 把固定下來的 HEAD 檢出到一個全新的暫存 worktree。之後每一步都在其中執行，並去除所有 `PYTEST_*`/`PYTHON*` 環境變數；閘門會確認 `leanc` 是從這個 worktree 匯入的。
3. 執行 `python -m scripts.constraints`。
4. 執行完整測試。junit 報告必須顯示測試數大於 0，且沒有失敗、錯誤或略過；出現略過代表缺少必要的工具。
5. 執行 `run_experiments.py`，必須回報 N/N 且 N > 0。
6. 檢查批准相容性：對每個自基準分支以來未改動的範例目錄，用基準程式碼做出的批准，在 HEAD 程式碼下必須仍然有效。
7. 確認 worktree 仍在固定的 HEAD 上且沒有被改動。

成功時輸出 `MERGE GATE PASS head=<sha> base=<sha>`。合併之前要先取得遠端，確認遠端基準分支仍等於輸出中的 base sha（否則重新執行閘門），然後只合併輸出中的 head sha（使用 `expectedHeadSha` 和 merge commit）。之後有任何新 commit，都必須重新執行閘門。

完整測試需要 clang-18、gcc、CBMC、bwrap，以及支援 AVX2 的 x86-64 CPU（SIMD 測試會執行 AVX2 程式碼）。WP 測試以及 `wp` 規格的完整性測試需要支援 `-print-machdep-builtin-macros` 的 Frama-C >= 27（已用 33 測試）；Lean 測試需要 Lean 4。缺少某個工具時相關測試會被略過，因此合併閘門需要全部工具。

## 限制

- `VERIFIED_BOUNDED` 受限於已批准的 harness；harness 是否忠實表達需求，仍需由人判斷。
- sanitizer 只涵蓋可信測試實際執行到的路徑。
- CBMC 不對無函式本體的函式（如 SIMD intrinsic）建模，也無法套用部分參數（如 `-ffast-math`、`-O2`、`-m*`）：前者使結果不確定，後者作為假設揭露。WP 不套用任何 `compile.flags`：它們會被揭露，而以 `-funsigned-char` 建置的程式碼在 Frama-C 有號 `char` 的 machdep 下仍可能得到 `PROVED`。
- 巨集比較只看按名稱使用的預先定義巨集。用記號拼接（`##`）拼出的預先定義巨集名，以及兩邊工具中由系統標頭定義且取值不同的巨集（例如 `M_PI`：glibc 在 `-std=gnu11` 下定義它，在 `-std=c17` 下不定義），都不會被比較：依據它們分支的程式碼可能讓編譯關卡與驗證器看到不同的程式碼。
- `compile.flags` 的檢查是一份拒絕清單，而不是允許清單。
- 堆疊框大小檢查以 `-O0` 度量每個函式。較高 `-O` 下的內聯可能合併堆疊框，呼叫鏈的總深度也不檢查。
- `PROVED` 只相對於已批准的 ACSL 契約成立，且以 WP 的記憶體與機器模型為前提。
- 修復政策是縱深防禦（文字規則）；真正的防線是受保護的 harness 加上獨立驗證器。
- C++20 沒有形式化關卡（CBMC 無法解析現代 C++，ESBMC 尚未整合），因此 C++ 的最佳結果是 `UNKNOWN`。
- Lean 證明的是模型，它與 C 程式碼之間只透過差分測試向量連結。
- bubblewrap 不是虛擬機；高風險工作請在獨立的容器或 VM 中執行。

更多文件（繁體中文）：[架構](docs/architecture.md) · [規格格式](docs/spec-format.md) · [狀態語意](docs/status-semantics.md) · [信任模型](docs/trust-model.md) · [安全性](docs/security.md) · [實驗](docs/experiments.md) · [Roadmap](docs/roadmap.md)
