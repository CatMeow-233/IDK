# Lean C

[English](README.md) · [简体中文](README.zh-Hans.md) · **繁體中文**

規格優先、由獨立驗證器裁決的 AI C17 程式碼生成（V2 另支援 C++20、Frama-C/WP、Lean 4）。研究原型。

程式碼由 AI 撰寫，但正確與否由獨立工具判斷：Clang、Clang Static Analyzer、ASan/UBSan/LSan、CBMC，以及可選的 CBMC 迴圈契約（opt-in 的 inductive profile）、Frama-C/WP 與 Lean 4。它們依據的是經人工批准並雜湊鎖定的規格。

```
需求 -> 規格草案（不受信任）-> 人工批准（雜湊鎖定、HMAC 簽章）
  -> 程式碼生成 -> 編譯 -> 完整性關卡 -> 靜態分析 -> sanitizer 測試
  -> CBMC ->（opt-in）inductive CBMC 迴圈契約 ->（V2）Frama-C/WP ->（V2，可選）Lean 4
  -> 反例 -> AI 修復（政策檢查，最多 N 回合）-> 重新驗證 -> JSON / Markdown 報告
```

目錄：[安裝](#安裝) · [快速開始](#快速開始) · [概念](#概念) · [指令](#指令) · [狀態與結束代碼](#狀態與結束代碼) · [關卡規則](#關卡規則) · [機器介面（AI agent / CI）](#機器介面ai-agent--ci) · [LLM provider](#llm-provider) · [設定](#設定) · [V2 工具](#v2-可選工具) · [範例](#範例) · [開發 Lean C](#開發-lean-c) · [限制](#限制)

---

## 安裝

需要 Linux 或 WSL2（以下指令以 Ubuntu 24.04 為例），以及 Python 3.11 以上。

```bash
sudo apt-get install -y clang-18 libclang-rt-18-dev llvm-18 cbmc gcc bubblewrap
pip install -e ".[dev]"          # 執行相依：pydantic、PyYAML；開發：pytest
leanc doctor                     # 實測 clang、分析器、sanitizers、gcc、CBMC、goto-cc、goto-instrument、bwrap 沙箱與網路隔離、LLM 設定
```

`leanc doctor` 會實際執行每個工具，並為每項失敗的檢查給出修復方式；有必要的檢查失敗時以 exit 30 結束。加上 `--json` 可取得機器可讀輸出（`{"ok": bool, "checks": [{name, ok, required, detail, fix}]}`）。若 bubblewrap 沙箱不可用，sanitizer 測試會被略過，整體狀態最多只能是 `UNKNOWN`（見 [安全性](docs/security.md)）。

Ubuntu 24.04 的 `cbmc` 套件是 CBMC 5.95.1，並同時安裝 `goto-cc` 與 `goto-instrument`；只有 opt-in 的 `inductive` 關卡用到它們。doctor 中的 `goto-cc (inductive gate)`、`goto-instrument (inductive gate)` 與 `cbmc release pin (inductive gate)` 三列不是必要檢查，因此永遠不會造成 exit 30：它們回報這兩個工具是否找得到，以及 `cbmc`、`goto-cc`、`goto-instrument` 是否都輸出經過測試的版本標記 `(cbmc-5.95.1)`。否則 `inductive` 關卡為 `ERROR`。

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
| 專案根目錄 | 最近一個含 `leanc.yaml` 的上層目錄，否則為規格檔所在目錄；可用 `--project-root` 覆寫。`spec draft` 沒有規格檔，未指定 `--project-root` 時使用目前目錄 |
| 規格（`spec.yaml`） | 可信契約：目標簽名、契約、來源檔、編譯/分析/測試/CBMC 設定、必要 `gates`。格式見 [docs/spec-format.md](docs/spec-format.md) |
| 受保護檔 | 規格、`sources.headers`、`verification.harness`、`tests.driver`、`inductive.harness`，以及 V2 的 ACSL 標頭、Lean spec/vectors。生成與修復永遠不會寫入這些檔案 |
| 可編輯檔 | `sources.editable`，規格有相應區段時另有 `lean.proofs` 與 `inductive.contracts`：修復唯一能寫入的檔案（`generate` 只寫 `sources.editable`）。它們不計入批准的雜湊 |
| 批准 | `leanc spec approve` 把規格的語意雜湊與每個受保護檔的 SHA-256 綁進一筆紀錄，並用 `LEANC_HOME`（預設 `$XDG_CONFIG_HOME/leanc`，未設定 `XDG_CONFIG_HOME` 時為 `~/.config/leanc`）中的金鑰簽章。任何變更都會導致 `SPEC_UNAPPROVED`，直到重新批准為止，而且 `version` 必須遞增。回滾帳本會拒絕已被取代的批准 |
| 規格草案 | `leanc spec draft` 寫入 `drafts/<id>/`，並標記為 `provenance: ai-draft`。在人工審查並批准之前，它永遠不受信任 |
| 關卡（gate） | 一個驗證階段：`compile`、`integrity`（強制；在編譯關卡通過後執行）、`static_analysis`、`sanitizer`、`cbmc`、opt-in 的 `inductive`，V2 另有 `wp`、`lean`。必須每個必要關卡都執行且通過，才算成功 |
| 執行（run） | 每次 `generate` / `verify` / `repair` / `run` 都會建立 `.leanc/runs/<run_id>/`。`spec draft` 會建立 `.leanc/runs/draft-<UTC 時間戳>-<hex>/`，用來存放它的 `leanc.log` 以及真實 provider 的 `llm/` transcript；除非指令中止（見 exit 2），其中不會寫入 `run.json`，所以 `report latest` 會略過它 |

給所有 agent 的規則：不得修改受保護檔，也不得代替人執行 `spec approve`。批准就是信任邊界。

## 指令

| 指令 | 作用 | 建立 run |
|---|---|---|
| `leanc doctor [--no-sandbox]` | 檢查工具鏈、沙箱與 LLM 設定（`--no-sandbox` 略過沙箱檢查） | 否 |
| `leanc init [DIR] [--template safe-array-get] [--force]` | 由範本建立專案 | 否 |
| `leanc spec draft --id ID (--requirement TEXT \| --requirement-file F) [--title T] [--force]` | 需求 → `drafts/<id>/` 中不受信任的草案（exit 40） | 僅日誌目錄 |
| `leanc spec approve SPEC --by NAME [--note N] [--yes]` | 人工批准（雜湊鎖定 + 簽章） | 否 |
| `leanc spec status SPEC` | 批准狀態、簽章有效性、批准後的變更（未批准時 exit 40） | 否 |
| `leanc spec show SPEC` | 正規化規格、雜湊、有效的 CBMC 檢查 | 否 |
| `leanc generate SPEC [--force]` | 為已批准的規格寫入 `sources.editable`（exit 0 表示已生成，**尚未**驗證；記錄的 `status` 是 `UNKNOWN`） | 是 |
| `leanc verify SPEC` | 對目前的原始碼執行各驗證關卡 | 是 |
| `leanc repair SPEC [--max-rounds N] [--no-write]` | 先驗證，再進行反例驅動的修復迴圈（`--max-rounds`：0 到 20） | 是 |
| `leanc run SPEC [--regenerate] [--no-repair] [--max-rounds N] [--no-write]` | 規格檢查 → 生成（若不存在）→ 驗證 → 修復 → 再驗證 → 報告（`--max-rounds`：0 到 20） | 是 |
| `leanc report [RUN\|latest] [--format md\|json]` | 顯示某次執行的報告；以該次執行的狀態作為 exit code；若成功的執行之後檔案被修改，顯示 STALE 並回傳 20；狀態為 `UNKNOWN`（已生成、尚未驗證）的 `generate` 執行回傳 0 | 否 |

共用選項：

| 選項 | 適用指令 | 作用 |
|---|---|---|
| `--json` | `report` 以外全部 | stdout 只輸出單一 JSON 物件，日誌寫到 stderr |
| `-v` | `report` 以外全部 | 詳細日誌寫到 stderr |
| `--project-root DIR` | spec、generate、verify、repair、run、report | 覆寫專案根目錄 |
| `--gates a,b` | verify、repair、run | 只執行規格關卡的子集（結果最多 `UNKNOWN`） |
| `--fail-fast` | verify、repair、run | 第一個未通過的關卡之後就停止 |
| `--timeout-scale X` | verify、repair、run | 所有工具逾時乘以 X（0.01 到 100） |
| `--no-sandbox` | verify、repair、run | 不使用 bwrap；動態測試會 `SKIPPED`，結果最多 `UNKNOWN` |
| `--allow-unsandboxed` | verify、repair、run | 危險：在沒有沙箱的情況下執行不可信的測試程式（只有這個旗標能這樣做，`leanc.yaml` 不行） |
| `--check-unapproved` | verify、repair、run | 對未批准的規格執行關卡，結果僅供參考；狀態仍是 `SPEC_UNAPPROVED` |
| `--provider mock\|openai`、`--mock-dir D`、`--llm-timeout S` | spec draft、generate、repair、run | 選擇 LLM |

## 狀態與結束代碼

| exit | 狀態 | 意義 |
|---|---|---|
| 0 | `PROVED` | 必要的 `wp` 關卡證明了已批准的 ACSL 契約與所有 RTE 守衛（smoke test 無異常），且 WP 套用了每一項有語意影響的 `compile.flags` 以及建置的 `char` 有號性。這是 `PROVED` 唯一的來源 |
| 0 | `VERIFIED_INDUCTIVE` | opt-in：滿足 `VERIFIED_BOUNDED` 的全部條件，並且必要的 `inductive` 關卡通過。CBMC 以 DFCC 迴圈契約證明：受保護的 inductive harness 中照原樣撰寫的 `main()` 的每一次執行（每條路徑最多呼叫一次 claimed 函式，即 `inductive.functions` 中的函式），都滿足在該路徑上執行到的 harness 斷言，且只檢查強制檢查類別；每個帶契約的迴圈都以歸納法證明（進入時與一步之後的 invariant、assigns、decreases）；非 const 的 static 物件從任意值開始，記憶體配置總是成功。harness 的 `LEANC_ASSUME` 只是涵蓋狀態的上界（harness 的其他程式碼也會縮小它們，且不回報）；沒有被 claim 的函式與額外的 `verification.checks` 類別只有有界結果；已知缺口見[限制](#限制)。**永遠不會升級為 `PROVED`** |
| 0 | `VERIFIED_BOUNDED` | 在已批准的 harness、輸入界限與 unwind 界限內沒有違反；所有迴圈都已完全展開；harness 不是 vacuous。**永遠不會升級為 `PROVED`** |
| 10 | `COMPILE_ERROR` | 編譯錯誤（或在 `warnings_policy: block` 下出現警告） |
| 11 | `STATIC_ANALYSIS_WARNING` | 在 `policy: block` 下有分析器發現，或違反完整性關卡 |
| 12 | `RUNTIME_TEST_FAILED` | sanitizer 回報、exit code 或輸出錯誤，或 Lean 測試向量不符 |
| 13 | `COUNTEREXAMPLE_FOUND` | CBMC 找到具體反例（真實的執行路徑，即使 unwind 不足也成立），或 Frama-C/WP 的證明器判定某個證明目標不成立（該失敗帶有目標的位置，但沒有輸入或路徑）。`inductive` 關卡永遠不會回報反例 |
| 20 | `UNKNOWN` | 無法下結論：unwind 不足、harness vacuous、關卡被略過、只跑關卡子集、沒有形式化關卡（C++）、目標未證明、WP 證明所依據的 `compile.flags` 或建置目標的 `char` 有號性未被 Frama-C 套用、`inductive` 關卡為 `INCONCLUSIVE`（迴圈沒有契約、迴圈契約被拒絕或未證明、性質未證明、探針不可達、函式未宣告） |
| 21 | `TIMEOUT` | 必要工具逾時 |
| 30 | `TOOL_ERROR` | 工具崩潰或輸出無法解析，或 `inductive` 關卡為 `ERROR`（CBMC 工具不是經過測試的版本、違反 harness 規則或可信符號規則）；另外，已建立的 provider 在生成過程中出錯或生成的程式碼被拒絕時（`generate`、`run`），`run` 無法為生成建立 provider 時，以及需要修復但無法建立 provider 時（`repair`、`run`；驗證的判定保留在 `initial_status` / `verification.status`，原因在 `messages`），也是這個狀態 |
| 40 | `SPEC_UNAPPROVED` | 目前的規格與受保護檔沒有有效批准 |
| 50 | `REPAIR_EXHAUSTED` | 修復迴圈結束但沒有成功；修復迴圈不改動工作區 |
| 2 | — | 使用方式或輸入錯誤。若指令此前已建立執行目錄，該次執行會以狀態 `TOOL_ERROR` 記錄，`report` 對它回傳 30 |
| 70 | — | 內部錯誤（traceback 寫到 stderr） |
| 130 | — | 被中斷（Ctrl-C） |

「沒找到反例」不等於成功。逾時、工具錯誤、無法解析的結果、被略過的關卡，都不算成功。必要的 `inductive` 關卡未通過時，結果最好也只是 `UNKNOWN`；所有必要關卡都通過時，必要的 `wp` 關卡給出 `PROVED` 而不是 `VERIFIED_INDUCTIVE`。彙總規則見 [docs/status-semantics.md](docs/status-semantics.md)。

## 關卡規則

這些規則堵住了「關卡通過、但程式碼其實沒被真正檢查」的漏洞；仍未堵住的列在[限制](#限制)中。它們都不會把失敗變成成功。

| 規則 | 行為 |
|---|---|
| 完整性未通過 | `sanitizer`、`cbmc`、`inductive`、`wp`、`lean` 只在完整性關卡通過後執行，否則為 `SKIPPED`（報告原文：「never executed, model-checked or proved」）；`gates` 不含 `compile` 時同樣如此，因為完整性關卡在編譯關卡通過後才執行。`static_analysis` 照常執行。狀態取自完整性關卡：`FAIL` 得 `STATIC_ANALYSIS_WARNING`，`ERROR` 得 `TOOL_ERROR`，`TIMEOUT` 得 `TIMEOUT`；完整性關卡沒有執行時，狀態為 `UNKNOWN`。修復迴圈中，讓完整性通過的候選算作進展，即使隨後解鎖的關卡找到反例，前提是先前已執行的關卡（`compile`、`static_analysis`）沒有失去通過、也沒有新增失敗 |
| 堆疊框大小 | 完整性關卡以 `-fstack-usage` 編譯每個可編輯檔案（先以 `-include` 加入一個錨點函式，再加去掉警告控制後的 `compile.flags`，最後是 `-O0 -fno-lto -fembed-bitcode=off`），讀取 clang 寫出的堆疊框大小：超過 1 MiB 的堆疊框為 `STACK_FRAME_TOO_LARGE`，clang 標為 `dynamic` 的堆疊框（執行期才配置的堆疊：VLA、`alloca`、`_alloca`）為 `UNBOUNDED_STACK`。錨點讓 clang 為每個產生程式碼的檔案寫出堆疊用量檔；沒有寫出時（例如 `-emit-llvm`）或編譯的結束碼不是 0，完整性關卡為 `ERROR`。VLA / `alloca` 檢查（`UNBOUNDED_STACK`）對每個檔案編譯三次。第一次帶上 `compile.flags`，失敗即關閉：只有每一行錯誤都是解析得出的 VLA / `alloca` 診斷時才算通過。第二次把 `compile.flags` 中的系統 include 選項（`-isystem`、`-idirafter`、`-iwithsysroot` 等）改成放在規格 include 目錄之後的 `-I`，保持它們在 `compile.flags` 中的順序，並去掉 `--system-header-prefix=`；`-idirafter` 的目錄因此會先於工具鏈自己的標頭被搜尋，所以這次執行可能在正式建置能編譯的情況下編譯不過。第三次不帶 `compile.flags`，與舊版相同，同樣失敗即關閉，所以只靠某個 `compile.flags` 項才能編譯的檔案會得到 `ERROR`：include 目錄請放在 `sources.include_dirs`，巨集請放在 `compile.defines`。只有第二次執行只增加違規而不會失敗：它編譯不過時，它輸出的 VLA / `alloca` 診斷仍算作違規，其他錯誤行被忽略；但它逾時或編譯器無法執行時仍然算數。因此系統 include 選項、module map 與強制引入標頭中的 pragma，都無法隱藏不帶它們時會被回報的 VLA，而編譯器自己的系統標頭（libstdc++）仍是系統標頭 |
| 三字元序列 | 可編輯的 C 程式碼中任何位置（包括註解與字串常值）出現三字元序列（`??/`、`??=` 等）即為 `TRIGRAPH`：clang 在 ISO C 模式（`-std=c17`）下以及 Frama-C（它的 gcc 以 `-std=c11` 執行）會替換三字元序列，CBMC 以 gcc 的 gnu 模式前處理則不會，所以註解結尾的 `??/` 只會對其中一方隱藏下一行。原始字串前綴（`R"`、`u8R"`、`LR"` 等）為 `TOOL_DIVERGENCE`：gcc 的 gnu 模式把它讀成原始字串常值，clang 在 C 中讀成識別字加字串。`__FILE__` 也是 `TOOL_DIVERGENCE`，因為 `-fmacro-prefix-map=` 只改變 clang 的值 |
| 型別選擇與隱藏呼叫 | 完整性關卡在可編輯程式碼中以及巨集展開之後（因此受保護標頭檔的巨集在可編輯程式碼使用處也會被抓到）拒絕：`_Generic`、`__builtin_types_compatible_p`、`__builtin_classify_type` 為 `TOOL_DIVERGENCE`（CBMC 5.95.1 與 clang 可能選擇不同的關聯：`long x` 時 `_Generic(x, long: 1, long long: 2)` 在 clang 下為 1、在 CBMC 下為 2）；`mode` 屬性為 `TOOL_DIVERGENCE`（CBMC 忽略它：`mode(QI)` 型別在原生建置中為 1 位元組，在 CBMC 中為 4 位元組）；`cleanup` 屬性為 `CONSTRUCTOR`（CBMC 與 Frama-C 都不會呼叫清理函式，所以其中的存取從未被檢查） |
| 自我迴圈（CBMC） | verify 與 vacuity 執行在 `--unwinding-assertions` 之後傳入 `--no-self-loops-to-assumptions`。沒有它時，CBMC 會把跳回自己的迴圈（`for (;;) {}`、`while (1);`、`L: goto L;`、`while (k > K) {}`）變成假設並丟棄到達它的路徑，於是在允許的輸入上原生會卡住的程式碼也會得到 `VERIFIED_BOUNDED`；有了它，這種迴圈會使展開斷言失敗（`INCONCLUSIVE`，狀態 `UNKNOWN`） |
| vacuity 建置（CBMC） | 在任何 CBMC 執行之前，每個可編輯原始檔與 harness 都在沒有 `-DLEANC_VACUITY_CHECK` 的情況下前處理一次（`cbmc --preprocess`，使用 `SOURCE_DATE_EPOCH=0`，所以 `__DATE__` 與 `__TIME__` 是固定的），之後若有 vacuity 執行，再在有該巨集的情況下前處理一次。文字寫入 `build/cbmc/verify/` 與 `build/cbmc/vacuity/`（`<path>.i`），verify、coverage 與 vacuity 執行只讀取這些檔案，所以不論 harness 從時鐘或檔案系統讀到什麼（例如對執行輸出使用 `__has_include`），每次執行檢查的都是比較過的文字。`cbmc --preprocess` 即使前處理器失敗也以 0 結束，所以 stderr 上的 `PREPROCESSING ERROR` 行（`#include` 了不存在的檔案、`#error`）、不是有效 UTF-8 的輸出（例如常值中的 Latin-1 位元組 `é`：`.i` 檔保存的位元組會與被編譯的原始檔不同）以及沒有原始檔本身 linemarker 的輸出，同樣是 `ERROR`（`TOOL_ERROR`）。CBMC 自帶的函式庫模型以 CBMC 的預設設定前處理：`compile.flags` 與 `compile.defines` 中的 `-D` 與 `-I` 只作用於專案的程式碼，所以模型看不到對它所檢查之巨集的 `-D`（例如 `MAP_UNINITIALIZED`，`mmap` 模型把它當作 0），也看不到 `-I` 用來取代系統標頭的標頭。把程式碼位置上的每個 `LEANC_REACH` 展開（同一行上的 `__CPROVER_assert(0, "leanc_reach:" "label")`，字串常值內的文字不算，經由 `.` 或 `->` 呼叫的也不算）替換為 `((void)0)` 之後，一個單元的兩份文字必須完全相同。否則關卡為 `ERROR`（`TOOL_ERROR`），且不啟動任何 CBMC 執行：直接或透過字串化探針依據 `LEANC_VACUITY_CHECK` 分支的 harness，會顯示到達探針的是另一個程式，而不是被驗證的程式。vacuity 文字中的每個這種位置，都需要在它的檔案、行與標籤上有一個屬於它自己的已回報探針（CBMC 丟棄的程式碼裡透過別名寫的探針不會被回報）；沒有的位置就是 vacuous harness（`INCONCLUSIVE`，狀態 `UNKNOWN`） |
| `__CPROVER_` 名稱（CBMC） | verify 執行之後，前處理單元的 `cbmc --show-symbol-table`（若之後有 vacuity 執行則用 vacuity 單元，因為只在探針位置使用的名稱在 verify 單元中未被使用而被丟棄）中不能有位於原始檔裡、名稱以 `__CPROVER_` 開頭的符號（函式、參數、區域變數、全域變數、typedef；看基本名稱或最後一個 `::` 部分）。參數 `void (*__CPROVER_assert)(int, const char *)`、這種型別的區域變數或 `__CPROVER_assert` 的定義，會讓 `LEANC_REACH`、`LEANC_ASSUME` 與 Lean C 插入的檢查呼叫 harness 程式碼；這樣的程式為 `ERROR`（`TOOL_ERROR`）。歸納關卡對它兩個建置的連結後符號表套用同一規則。區塊範圍內重新宣告內建函式也會被拒絕 |
| 預先定義的巨集 | 僅適用於含 `cbmc` 或 `wp` 的規格。把 clang 在編譯關卡選項（`-std`、`compile.flags`、`compile.defines`）下預先定義的巨集，與 gcc 在各驗證器自身前處理方式下預先定義的巨集比較。CBMC：`-D__CPROVER__ -m64`、`-funsigned-char`、`-std=gnuNN`、它收到的 `-D` 定義，然後是它的 x86-64 定義（`__LP64__`、`__x86_64__` 等），順序與 CBMC 呼叫 gcc 時相同。Frama-C：`-undef -nostdinc` 加 machdep 的內建巨集（`frama-c -print-machdep-builtin-macros`，其中定義了 `unix` 與 `linux`）、`-D__FRAMAC__ -std=c11` 與 `compile.defines`。可編輯程式碼中用到取值不同的巨集即為 `TOOL_DIVERGENCE`。需要 `gcc`，`wp` 還需要 `frama-c`；缺少時完整性關卡為 `ERROR` |
| `compile.flags` | 每一項都是一個選項，值直接接在選項後面（`-Iinclude`、`-DNAME=1`、`-std=c17`、`-includepre.h`）。規格驗證拒絕輸入檔、`-` 與 `--`。完整性關卡會詢問 clang（`clang -### <項>`）某一項是否把下一項當作自己的值（`-I -Wno-such-dir`、`-iprefix -funsigned-char`）；這樣的項得 `TOOL_ERROR`，因為 CBMC 轉換與堆疊檢查把每一項都當作獨立選項讀取。另外拒絕：`-fsanitize*` / `-fno-sanitize*`（sanitizer 由 `tests.sanitizers` 決定），以及把參數交給內部工具或替換工具鏈的選項：`-X...`（`-Xclang`、`-Xanalyzer`、`-Xlinker` 等）、`--for-linker`、`-mllvm`、`-Wp,` / `-Wa,` / `-Wl,`、`--analyze...`、`@file`、`--config`、`-fplugin` / `-fpass-plugin`、`-ccc-*`、`-B` / `--prefix`、`--ld-path` / `-fuse-ld=<path>`、`-resource-dir` / `--resource-dir`、`--sysroot` / `-isysroot`、`-ivfsoverlay` / `-vfsoverlay`、`--gcc-toolchain` / `--gcc-install-dir`、`-nostdlib` / `--no-standard-libraries` / `-nodefaultlibs` / `-nostartfiles`、`--driver-mode`。改變前處理器輸出或在產生程式碼之前就停止的選項也會被拒絕，因為完整性關卡以 `compile.flags` 讀取前處理後的原始碼：`-E`、`-P` / `--no-line-commands`、`-C` / `-CC`、`-d*`（`-dM`、`-dD` 等）、`-M` / `-MM`、`-fdirectives-only`、`-frewrite-includes`、`-fuse-line-directives`、`-fkeep-system-includes`、`-fminimize-whitespace`、`-traditional-cpp`、`-fsyntax-only`、`-fdriver-only`；前處理輸出中沒有該檔案的 linemarker 時為 `ERROR`。改變 C 原始碼斷詞方式的選項也會被拒絕，因為 CBMC 以 gcc 的 gnu 模式（最高 gnu11）前處理，Frama-C 以 gcc 的 `-std=c11` 前處理：strict C89/C90（`-std=c89`、`-std=c90`、`-std=iso9899:1990`、`-std=iso9899:199409`、`-ansi`，它們沒有 `//` 註解）、C23（`-std=c23`、`-std=c2x`、`-std=gnu23`、`-std=gnu2x`，它們有數字分隔符），以及 `-fdigraphs` / `-fno-digraphs`。`-Wwrite-strings` 會被拒絕：clang 把它轉成 `-fconst-strings`，讓字串常值的型別變成 `const char[N]`，而沒有任何驗證器套用這一點。靜態分析收到其餘參數。CBMC 會收到 `-D`、`-I`、`-std=` / `--std=` 與 `-funsigned-char`（有號 `char` 是 CBMC 在 x86 上的預設），GCC 的長拼法（`--define-macro=`、`--undefine-macro=`、`--include-directory=`、`--unsigned-char`、`--signed-char`）也一樣，但絕不會收到 `NDEBUG`。`-U` 只能取消它之前的 `-D`（函式型巨集也一樣）；其他 `-U` 會被列為未套用。警告與 `-g` 參數沒有語意影響，但 `-Wdeprecated` / `-Wno-deprecated` 除外：它們在 C++ 中決定 `__DEPRECATED`，因此會保留給堆疊檢查。其餘參數（例如 `-O2 -ffast-math -mavx2`）列在假設「compile.flags not applied by CBMC」中；CBMC 的判定不變。對 WP 而言，每一項要麼被套用，要麼沒有語意影響（警告與 `-g` 參數，同上），要麼未被套用。只有當建置的 `char` 有號性與 Frama-C machdep 的相同（`x86_64` 上為有號）時，char 有號性才算被套用。建置的有號性取最後一個 char 有號性參數（`-fsigned-char`、`-fno-unsigned-char`、`--signed-char` 為有號），否則取 `clang <compile.flags> -dumpmachine` 輸出的建置目標的預設值：架構 `x86_64`、`i386`、`x32`（CBMC 使用的同一張表）為有號，其他架構視為未知是否有號。沒有 char 參數時，未知是否有號的目標（如 `aarch64-unknown-linux-gnu`）會加入未套用項 `(default char of build target ...)`；查詢失敗或無法解析的三元組無論參數如何都會加入 `(build target unknown: ...)`。最後一個 `-std=` / `--std=` 選擇 C11 或 C17（`c11`、`c1x`、`iso9899:2011`、`c17`、`c18`、`iso9899:2017`、`iso9899:2018`）時該參數被套用；它與 `language.standard` 一樣以「Frama-C preprocesses and parses the code as c11」揭露。`gnu*` 與其他標準不被套用。其餘各項都未被套用，包括 `-D`、`-U` 與 `-I`（Frama-C 只收到 `sources.include_dirs` 與 `compile.defines`）。未被套用的項列在「compile.flags not applied by Frama-C」中（`wp` 階段的 `details.compile_flags`）。只要有其中任何一項，證明就是 `wp` 關卡的 `INCONCLUSIVE`（`UNKNOWN`），絕不是 `PROVED`，而 Frama-C 模型下的結果保留在 `details.frama_c_model_verdict` 中。此時不會嘗試證明修復 |
| 無函式本體的函式（CBMC） | 允許：`nondet_*`、`__CPROVER*`、`__builtin_prefetch`、`__builtin_expect`。其他無函式本體的被呼叫函式會使 `cbmc` 無法通過：`<immintrin.h>` 背後的 SIMD builtin，以及完整性關卡允許、但 CBMC 沒有模型的 `memchr`、`strnlen`、`strstr`、`fputc`、`putc`。路徑用到這類函式回傳值的違規是 `unmodelled-callee` 診斷，不是反例；其他違規仍是反例。沒有反例時關卡為 `INCONCLUSIVE`（狀態 `UNKNOWN`）：把呼叫改寫成一般 C 才能得到判定 |
| 未宣告的函式（CBMC） | CBMC 把對未宣告函式的呼叫視為回傳 `int`，因此未宣告的 `nondet_*` 輸入只會取到轉換成呼叫端型別的 `int` 值。`function 'f' is not declared` 警告是 `undeclared-function` 診斷，會使 `cbmc` 無法通過（`INCONCLUSIVE`，狀態 `UNKNOWN`）；違規仍是反例。`leanc.h` 宣告了 `nondet_int`、`nondet_uint`、`nondet_long`、`nondet_ulong`、`nondet_llong`、`nondet_size_t`、`nondet_char`、`nondet_uchar`、`nondet_short`、`nondet_bool`、`nondet_int32_t`、`nondet_uint32_t`、`nondet_int64_t`、`nondet_uint64_t`：其他 `nondet_*` 請在 harness 中宣告 |
| CBMC 內部錯誤 | 不變式違規報告為 `TOOL_ERROR`（「CBMC internal error」），並附上 File/Condition/Reason 摘錄，而不是記憶體不足提示 |
| 契約安全檢查 | `contract.safety` 中的檢查會加入 CBMC 的性質檢查；規格雜湊不變 |
| sanitizer 建置 | 測試、所有權追蹤和 Lean 向量的建置都在 `compile.flags` 之後加上 `-fno-builtin -fno-optimize-sibling-calls`，這樣 clang `-O1` 無法刪除未使用的配置（`malloc`、`strdup`、`aligned_alloc`、C++ `new`），也無法刪除包裝函式的堆疊框 |
| 記憶體檢查的侷限 | `limitations` 列出洩漏檢查看不到的部分；只有關卡得出判定（`PASS` 或 `FAIL`）的檢查才算數。LeakSanitizer 只回報不可達的區塊。沒有 CBMC `memory-leak` 判定時，只在測試路徑上檢查洩漏。CBMC 不檢查對齊、嚴格別名、`restrict`，也不檢查同一配置內子物件之間的溢位 |
| `inductive` 關卡（opt-in） | 在 `cbmc` 之後執行，前提是規格的 `gates` 列出 `inductive`；此時規格驗證器要求有 `inductive` 區段、`cbmc` 關卡以及 `verification.malloc_may_fail: false`。與 `cbmc`、`wp`、`lean` 一樣，它只在完整性關卡通過、並且受保護檔再次與批准比對之後執行。它需要 `cbmc`、`goto-cc` 與 `goto-instrument`（`LEANC_CBMC`、`LEANC_GOTO_CC`、`LEANC_GOTO_INSTRUMENT`），三者的版本行都恰好有一個 `(cbmc-X.Y.Z)` 標記，且都是同一個經過測試的版本（只有 5.95.1），`goto-cc` 必須以 gcc 身分執行（`gcc (goto-cc ...`）；否則為 `ERROR`。該關卡的每次工具執行（前處理、goto-cc、goto-instrument、兩次 CBMC 執行）都受 `verification.timeout_s` × `--timeout-scale` 與 `verification.memory_mb` 限制；逾時就是 `TIMEOUT`，永遠不是部分通過。目標函式必須被 claim，這可能使關卡變慢（`examples/inductive_sponge` 約 70 秒） |
| inductive harness | `inductive.harness` 受保護。它的 `main()`（只定義一次，且在該檔案中）以 `LEANC_ASSUME` 在任意狀態上假設狀態不變量，直接呼叫 claimed 函式，在呼叫之後斷言，並至少含有一個 `LEANC_REACH`。Lean C 對自己的副本插樁，以下每一項都是 `ERROR`：某條路徑在 harness 的一次 claimed 呼叫之後又到達第二次 claimed 呼叫（由插入的斷言逐路徑檢查，因此沒有 claimed 函式會從另一個函式的結束狀態開始）；在 claimed 呼叫之後出現 `LEANC_ASSUME` 或 `__VERIFIER_assume`（同一函式內靜態檢查，並逐路徑檢查），或在假設中呼叫 claimed 函式；claimed 函式以直接呼叫以外的方式使用，或從未被呼叫；在函式本體中參考 `main`；在進入 claimed 函式之前執行了可編輯原始碼中的任何函式（假設中的可編輯 predicate、setup 呼叫、claimed 呼叫的引數：每個可編輯函式都會被插入入口檢查）；`main()` 中除了唯一一個位於最後的頂層 `return` 以外的 `return`，或 `main()` 中的 `goto`；`main()` 中從最後一個 `LEANC_ASSUME`（沒有就從開頭）到第一次 claimed 呼叫結束之間的 `=`、複合指定、`++` 或 `--`，一般宣告 `T x = ...;` 的 `=` 除外；harness 翻譯單元或其標頭中使用 `exit`、`abort`、`_Exit`、`quick_exit`、`longjmp`、`raise`、`err`、`errx`、`verr`、`verrx` 或其他會結束或跳出路徑的名稱，即使只是作為函式指標值；`leanc_` 名稱；讀取 inductive 執行會保留其初始值的可編輯物件（const，或含 const 成分）；`#include` 可編輯原始檔；有無 `-DLEANC_VACUITY_CHECK` 的兩次前處理結果除 `LEANC_REACH` 展開外還有差異。不偵測：harness 的其他程式碼也會縮小涵蓋的狀態（宣告的初始值、最後一個 `LEANC_ASSUME` 之前的寫入、第一次 claimed 呼叫之後的寫入、包住斷言的分支、helper 函式），因此批准者必須審查整個 harness |
| inductive 建置 | 該關卡只處理每個可編輯單元、harness（有無 `-DLEANC_VACUITY_CHECK` 各一次）及其複製模型的 `cbmc --preprocess` 輸出（`SOURCE_DATE_EPOCH=0`）；stderr 上的 `PREPROCESSING ERROR` 行、不是有效 UTF-8 的輸出以及沒有該單元本身 linemarker 的輸出都是 `ERROR`。`inductive.contracts` 中的子句必須通過嚴格的文法（不允許呼叫、指定、`++` / `--`、`?:`、逗號、取址、`sizeof`、量詞，也不允許 `sources.editable` 中定義的巨集名或 `NULL`、`INT_MAX` 等標準巨集名；見 [docs/spec-format.md](docs/spec-format.md)），只有其完全加括號的呈現結果會被插入，位置是契約所在行上寫出的那一個帶 `{` 迴圈本體的 `for` / `while` 迴圈。其他位置，或無法編譯的子句（例如受保護標頭中的巨集名，在那裡不會被展開），都是 `contract_rejected`（`INCONCLUSIVE`）。`goto-cc --verbosity 2` 先編譯可信單元（複製模型，然後是 harness），再編譯可編輯單元。在連結後的符號表中，每個可信符號都必須仍綁定到它自己的定義，不得存在 `<可信名稱>$link<N>` 重新命名（兩個 static 之間也一樣：請為可編輯的那個改名），可編輯檔中的符號不得命名為 `leanc_...`，原始檔中也不得宣告 `__CPROVER_` 名稱；否則為 `ERROR`。goto-cc 的 `function 'f' is not declared` 警告是 `undeclared_function`（`INCONCLUSIVE`）。之後執行 `goto-instrument --dfcc main --apply-loop-contracts`（從不使用 `--loop-contracts-file`，也從不省略 `--dfcc`）；harness 與 CBMC 函式庫之外仍沒有契約的迴圈是 `loop_without_contract`（`INCONCLUSIVE`），且不執行 CBMC。`memcpy`、`memmove` 與 `memset` 被替換為模型：檢查來源與目的範圍（`memcpy` 還檢查不重疊），然後讓目的內容變成任意值；非 const 的 static 物件從任意值開始；記憶體配置永不失敗 |
| inductive 判定 | verify 執行是 `cbmc dfcc.goto --no-library <checks> --unwind U --unwinding-assertions --no-self-loops-to-assumptions [--object-bits N] --json-ui`，其中 U = max(`verification.unwind`, 最長的 assigns 清單 + 2)，且只有強制檢查類別（`bounds`、`pointer`、`div-by-zero`、`signed-overflow`、`pointer-overflow`、`undefined-shift`）：從任意狀態出發，合法的模運算與洩漏沒有意義，因此 `verification.checks` 與 `contract.safety` 的類別只保留有界結果。每個契約都必須對應恰好一個 CBMC 迴圈，且該迴圈具備全部五個類別 `loop_invariant_base`、`loop_invariant_step`、`loop_decreases`、`loop_assigns`、`loop_step_unwinding`（否則為 `ERROR`）；完全沒有這些性質的契約（其迴圈無法從 harness 到達）是 `contract_rejected`。vacuity 執行必須到達每個 `LEANC_REACH` 位置、每個 claimed 函式入口的 `leanc_function:<f>` 探針、每個帶契約迴圈本體開頭的 `leanc_loop:<file>:<line>` 探針，以及每次 claimed 呼叫之後的 `leanc_return:<f>` 探針；不可達的探針是 `vacuous_harness`。不成立的性質給出 `INCONCLUSIVE` 與原因代碼：`inductive_step_failed`、`suspected_non_termination`（decreases 子句，位於該迴圈）、`unwind_insufficient`、`unmodelled_callee` 或 `property_unproved`。這些失敗的 kind 是 `unproved`，永遠不是反例，因為每次 claimed 呼叫與每個迴圈都從任意狀態開始，而真實執行可能永遠到不了那個狀態。無法判定的性質、違反 harness 規則，或 CBMC 的性質清單中缺少 harness 斷言，都是 `ERROR`。該關卡的結果是 `PASS`、`INCONCLUSIVE`、`TIMEOUT` 或 `ERROR`，永遠不是 `FAIL`。必要的 `PASS` 加上 `cbmc` `PASS` 得到 `VERIFIED_INDUCTIVE`，它陳述的範圍列在 `assumptions` 中 |

## 機器介面（AI agent / CI）

使用 `--json`，並依 exit code 或 `status` 分支。以 exit 2、70 或 130 結束時，以及在 `spec approve` 的互動確認中拒絕確認（exit 40）時，不會輸出 JSON 物件，訊息寫到 stderr。成功的 `generate` 回報的 `status` 為 `UNKNOWN`，`exit_code` 為 0。`leanc verify SPEC --json` 的輸出如下：

```json
{
  "command": "verify", "status": "COUNTEREXAMPLE_FOUND", "exit_code": 13,
  "run_id": "verify-20261121T034210Z-7c8afd", "run_dir": ".../.leanc/runs/verify-...",
  "provider": null, "report": ".../report.md", "messages": ["..."],
  "verification": {
    "status": "...", "success": false, "spec_id": "...", "spec_version": 1, "spec_hash": "...",
    "approval": {"...": "..."},
    "stages": [{"stage": "cbmc", "outcome": "FAIL", "status": "...", "required": true, "invoked": ["cbmc"],
                "duration_s": 1.2, "summary": "...", "tools": ["cbmc"], "failures": 3}],
    "detected_by": ["asan", "cbmc"], "reasons": ["..."], "assumptions": ["..."], "limitations": ["..."],
    "reason_codes": [{"code": "gate_failed", "stage": "cbmc", "location": null, "detail": "..."}],
    "failures": [{
      "id": "sanitizer:index_equals_count:asan:heap-buffer-overflow", "tool": "asan", "stage": "sanitizer",
      "kind": "...", "property_id": "...", "location": {"file": "src/array_lookup.c", "line": 9, "column": 16, "function": "array_lookup"},
      "inputs": [{"name": "argv", "value": "index_equals_count", "type": null}],
      "description": "...", "raw_ref": "raw/...", "ownership_trace": null
    }]
  }
}
```

`reason_codes`（也在 `verification.json` 中，並列在 `verification.md` 與 `report.md` 的「Reason codes」下）在成功狀態下為空。否則它以未通過的必要階段解釋狀態（`SPEC_UNAPPROVED` 時以規格檢查解釋）；每一項有 `code`、`stage`、`location`（或 `null`）與 `detail`。有具體代碼的 `INCONCLUSIVE` 階段只列出具體代碼；其他結果先給出通用代碼（`gate_failed`、`gate_skipped`、`gate_timeout`、`tool_error`、`gate_inconclusive`）。具體代碼有 `unwind_insufficient`、`vacuous_harness`、`vacuity_unchecked`、`unmodelled_callee`、`undeclared_function`、`property_unproved` 與 `frama_c_model_only_proof`，`inductive` 關卡另有 `inductive_step_failed`、`suspected_non_termination`、`contract_rejected` 與 `loop_without_contract`；`no_formal_gate` 標記既沒有 `cbmc` 關卡也沒有必要 `wp` 關卡的 `UNKNOWN` 規格。請依 `status` 與 `exit_code` 分支；代碼說明原因。

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

修復 prompt 會以明確標籤顯示同樣的證據。它不會指出執行的是哪一行 `return`。受測程式碼執行在被追蹤的行程中，因此它能偽造或隱藏這些證據（例如透過繼承的檔案描述符向追蹤日誌附加行，或在 stderr 輸出探針標記或形似 LeakSanitizer 的行）：證據可能因此錯誤或缺漏，但判定不受影響。

agent 修正程式碼有兩種方式：

1. **外部 agent 迴圈。** 執行 `leanc verify SPEC --json`，讀取 `verification.failures`（位置、輸入、性質）與 `raw_ref`。只修改 `sources.editable` 列出的檔案（規格有時還包括 `inductive.contracts` 或 `lean.proofs`），然後再驗證。遇到 exit 0，或 40（需要人處理）、20/21/30（不是程式碼缺陷，請看 `reasons` 與 `reason_codes`；`loop_without_contract`、`contract_rejected` 與 `inductive_step_failed` 需要修改 `inductive.contracts`）時停止。
2. **內建迴圈。** 執行 `leanc run SPEC --provider openai --json`（或 `repair`）。Lean C 會呼叫模型、對每個補丁做政策檢查，並且只把所有必要關卡都通過的候選寫回：驗證通過的候選（`VERIFIED_BOUNDED` / `VERIFIED_INDUCTIVE` / `PROVED`），或者在既沒有 `cbmc` 也沒有 `wp` 關卡的規格中狀態仍為 `UNKNOWN` 的候選（`--no-write` 不把修復後的候選寫回；在 `run` 中，生成缺少的來源檔或以 `--regenerate` 重新生成時仍會寫入 `sources.editable`）。

修復政策會拒絕以下補丁：寫入可編輯檔以外的檔案、加入 `__CPROVER_*` / `LEANC_*` / `__builtin_assume`、刪除 `assert`、抑制警告或 sanitizer、`#include` 逃逸、`system`/`exec`/`fork`/`socket`、inline asm、把目標函式掏空，以及過大的補丁。重複的補丁會被判定為循環。結果變成 `UNKNOWN`/`TIMEOUT` 不算改善。使用 `inductive` 關卡時，契約檔必須通過子句文法（`LOOP_CONTRACT`）；在只剩證明關卡未通過、且其中包含 `inductive` 的回合，修改其他任何檔案都會在驗證前被拒絕（`CONTRACTS_ONLY_ROUND`）；而當 `inductive` 關卡在修復階段開始時沒有通過時，C 程式碼與開始時不同、且通過 `inductive` 關卡的候選會在驗證後被拒絕（`INDUCTIVE_CODE_EDIT`）：它永遠不會被接受或寫回，其原始檔留在 `repair/round-<n>/blocked-candidate/` 供人審查。修復衡量進展時，`inductive` 階段的原因代碼與失敗一起計算。

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
| `LEANC_ALLOW_INSECURE_HTTP=1` | 允許透過明文 `http://` 把 API key 送到非 loopback 的主機。未設定時，只要設定了 key（`OPENAI_API_KEY` 也算），這樣的 `LEANC_BASE_URL` 就會被拒絕；`localhost`、`*.localhost`、loopback IP 以及不帶 key 的端點一律允許 |
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

專案設定不能削弱檢查、界限、關卡或批准，也不能讓不可信的程式在沒有沙箱的情況下執行。`sandbox: none` 的作用與 `--no-sandbox` 相同：工具也不使用 bwrap，動態測試會 `SKIPPED`。若設定了 `allow_unsandboxed`，該值會被忽略並發出警告。

| 變數 | 意義 |
|---|---|
| `LEANC_HOME` | 批准金鑰與帳本目錄（預設 `$XDG_CONFIG_HOME/leanc`，未設定 `XDG_CONFIG_HOME` 時為 `~/.config/leanc`）；請放在工作區之外 |
| `LEANC_CLANG`、`LEANC_CLANGXX`、`LEANC_CBMC` | 工具路徑 |
| `LEANC_GOTO_CC`、`LEANC_GOTO_INSTRUMENT` | `inductive` 關卡使用的 `goto-cc` 與 `goto-instrument`；未設定時搜尋 `PATH`。它們與 `cbmc` 必須回報同一個經過測試的版本（5.95.1） |
| `LEANC_FRAMAC` | `frama-c`（>= 27，含 WP）；未設定時搜尋 `PATH` 與 `/opt/opam/*/bin` |
| `LEANC_LEAN` | `lean`（含 `leanchecker` 的 toolchain）；未設定時搜尋 `PATH` 與 `/opt/lean/*/bin` |

## V2 可選工具

| 功能 | 安裝 | 規格 | 對狀態的影響 |
|---|---|---|---|
| Frama-C/WP + ACSL | `sudo apt-get install -y opam z3 cvc5 && opam init -y && opam install -y frama-c alt-ergo`（Ubuntu 的 `frama-c-base` 不含 WP） | ACSL 契約寫在受保護標頭中，關卡 `wp` | `PROVED` 唯一的途徑；證明修復讓 AI 補上 loop invariant |
| Lean 4 | release toolchain 解壓到 `/opt/lean/<版本>`，或使用 elan | 受保護的模型與定理敘述；證明由 AI 撰寫；kernel + `leanchecker`；只接受標準公理；差分測試向量 | 模型層級：未證明 → `UNKNOWN`，向量不符 → `RUNTIME_TEST_FAILED`；永遠不會產生 `PROVED` |
| C++20 | `clang++`（clang-18 內含） | `language: {name: cpp, standard: c++20}` | 沒有可用的 C++ model checker，因此最佳結果是 `UNKNOWN` |
| Inductive profile（opt-in，C） | 除 `cbmc` 外不需要別的：Ubuntu 24.04 的套件就是 CBMC 5.95.1（唯一經過測試的版本），並附帶 `goto-cc` 與 `goto-instrument` | 受保護的 `inductive.harness`、可編輯的 `inductive.contracts`、`inductive.functions`（含 `target.function`），關卡 `inductive` 與 `cbmc` 並列 | `cbmc` 也通過時為 `VERIFIED_INDUCTIVE`（exit 0）；永遠不會產生 `PROVED`；證明無法下結論時為 `UNKNOWN`，此時修復只能改契約檔 |

`leanc doctor` 會回報上述每個工具。缺少某個工具時，相關測試會被略過，而不是失敗（缺少 `goto-cc` 或 `goto-instrument`，或者 CBMC 版本不是 5.95.1，會讓使用真實工具的 inductive 測試失敗，而不是略過）。

## 範例

每個 `examples/<name>/` 都包含 `spec.yaml`、受保護的 `include/`、附帶的（通常有缺陷的）`src/`、`mock/` fixture 以及 `expected.yaml`；有 `cbmc` 關卡的範例還有受保護的 `harness/`（兩個 C++ 範例沒有），有 `sanitizer` 關卡的範例還有受保護的 `tests/`（`vacuous_harness` 沒有）。`inductive_sponge` 另有受保護的 inductive harness `harness/sponge_inductive.c` 與可編輯的契約檔 `inductive/loops.json`。

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
| `inductive_sponge` | 正確；opt-in 的 `inductive` 關卡對所有不超過 2^48 的長度 claim `sponge_absorb` | `VERIFIED_INDUCTIVE` | `VERIFIED_INDUCTIVE`（沒有修復回合） |

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
- `leanc/`、`tests/`、`scripts/` 中不得有註解或 docstring，`tests/` 或 `scripts/` 下的 `fixtures/` 目錄除外。意圖要用名稱、常數、Enum 與 frozen dataclass 表達。
- 不得有 bare/`Exception`/`BaseException` handler（唯一的例外是 `leanc/cli.py` 的 `main()` 中的 `except Exception` / `except BaseException`，它以 exit 70 結束；那裡同樣不允許 bare `except:`），也不得吞掉例外。
- 每個 `.py` 檔最多 600 行（`fixtures/` 目錄同樣除外）。

| 套件 | 職責 |
|---|---|
| `foundation` | 狀態、資料模型、原因代碼、inductive 範圍文字、錯誤、雜湊、憑證遮蔽、工作區（路徑/符號連結安全）、沙箱、`run_command`（只接受 argv、資源限制） |
| `specs` | 規格 schema、載入、雜湊、批准與帳本 |
| `lang` | C / C++ / Lean lexer、迴圈子句文法 |
| `rules` | 關卡與修復政策共用的純函式信任規則（C、C++、ACSL、Lean、前處理器、迴圈契約與迴圈位置） |
| `toolchain` | 工具探測、CPROVER 版本鎖定、`BackendContext` |
| `reports` | JSON / Markdown 輸出 |
| `llm` | provider 介面、mock、OpenAI 相容、prompts、回應解析 |
| `backends` | 各關卡：clang、analyzer、integrity、sanitizer、cbmc、inductive、counterexample、wp、lean |
| `repair` | 套用補丁（fuzz 0）、生成與修復政策 |
| `pipeline` | 驗證控制器、原因代碼、生成、修復迴圈及其 inductive 防護、doctor、設定、指令實作 |
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
6. 檢查批准相容性：對每個自基準分支以來未改動的範例目錄，用基準程式碼做出的批准，在 HEAD 程式碼下必須仍然有效。至少要有一個範例目錄未改動，否則這一步失敗。
7. 確認 worktree 仍在固定的 HEAD 上且沒有被改動。

成功時輸出 `MERGE GATE PASS head=<sha> base=<sha>`。合併之前要先取得遠端，確認遠端基準分支仍等於輸出中的 base sha（否則重新執行閘門），然後只合併輸出中的 head sha（使用 `expectedHeadSha` 和 merge commit）。之後有任何新 commit，都必須重新執行閘門。

完整測試需要 clang-18、gcc、CBMC 5.95.1 及其 `goto-cc` 與 `goto-instrument`（使用真實工具的 inductive 測試會執行它們；缺少 goto 工具或使用其他版本會讓這些測試失敗，絕不略過）、bwrap、位於 /usr/ 的 llvm-symbolizer（llvm-18 套件），以及支援 AVX2 的 x86-64 CPU（SIMD 測試會執行 AVX2 程式碼）。WP 測試以及 `wp` 規格的完整性測試需要支援 `-print-machdep-builtin-macros` 的 Frama-C >= 27（已用 33 測試）；Lean 測試需要 Lean 4。缺少某個工具時相關測試會被略過，因此合併閘門需要全部工具。

## 限制

- `VERIFIED_BOUNDED` 受限於已批准的 harness；harness 是否忠實表達需求，仍需由人判斷。
- `VERIFIED_INDUCTIVE` 相對於照原樣撰寫的受保護 inductive harness：它涵蓋其 `main()` 每條路徑最多一次 claimed 呼叫的執行，且只針對強制檢查類別與 harness 斷言。harness 的 `LEANC_ASSUME` 是涵蓋狀態的上界；宣告的初始值、最後一個 `LEANC_ASSUME` 之前或第一次 claimed 呼叫之後的寫入、包住斷言的分支以及 helper 函式會進一步縮小它們，且不回報。斷言弱，結論就弱：`examples/inductive_sponge` 只斷言 `rc == SPONGE_OK` 與 `s->fill < SPONGE_RATE`，所以默默只吸收長輸入一部分的程式碼在那裡不會被發現。
- `inductive` 關卡的已知缺口：經由指標、從成員陣列溢出到同一物件相鄰欄位或填充位元組的存取（`p->a[i]`、指標運算、`q = p->a; q[i]`、複製模型），以及複製到字串常值，都不會被回報（有界的 `cbmc` 關卡有同樣的缺口；具名物件上的 `s.a[i]` 會被檢查）。呼叫含迴圈的 CBMC 函式庫模型（`strlen`、`strcpy`、`strncpy`、`strncat`）會使迴圈契約的寫入集檢查失敗，因此這樣的程式碼永遠無法通過該關卡（不完備，但永遠不是錯誤的通過）。
- `inductive` 關卡的修復規則（`CONTRACTS_ONLY_ROUND`、`INDUCTIVE_CODE_EDIT`）只限制修復代理：人寫的、在大尺寸下放棄正確性的程式碼，在弱 harness 下仍可能得到 `VERIFIED_INDUCTIVE`；而使該關卡通過的正確程式碼修正必須由人來套用。
- sanitizer 只涵蓋可信測試實際執行到的路徑。
- CBMC 不對無函式本體的函式（如 SIMD intrinsic）建模，也無法套用部分參數（如 `-ffast-math`、`-O2`、`-m*`）：前者使結果不確定，後者作為假設揭露。WP 在 Frama-C 的模型下證明程式碼：Frama-C 未套用的有語意影響的 `compile.flags` 項（如 `-funsigned-char`、`-O2`、`-fwrapv`、`-D`）會使結果無法成為 `PROVED`，因此這樣的規格最多是 `UNKNOWN`。預設 `char` 未知是否有號的建置目標（如 aarch64，也包括 `char` 實為有號的 arm64 Apple 目標）需要明確的 `-fsigned-char` 才能得到 `PROVED`。與建置目標比較的只有 `char` 有號性：與 Frama-C `x86_64` machdep 的其他機器模型差異（型別大小、`long double`、對齊）不檢查。`language.standard` 只揭露、不檢查：Frama-C 以 C11 解析，那裡的其他標準不阻止 `PROVED`。
- 巨集比較只看按名稱使用的預先定義巨集。用記號拼接（`##`）拼出的預先定義巨集名，以及兩邊工具中由系統標頭定義且取值不同的巨集（例如 `M_PI`：glibc 在 `-std=gnu11` 下定義它，在 `-std=c17` 下不定義），都不會被比較：依據它們分支的程式碼可能讓編譯關卡與驗證器看到不同的程式碼。
- `compile.flags` 的檢查是一份拒絕清單，而不是允許清單。
- 堆疊框大小檢查以 `-O0` 度量每個函式。較高 `-O` 下的內聯可能合併堆疊框，呼叫鏈的總深度也不檢查。受保護標頭若依 `__OPTIMIZE__` 選擇函式本體，度量的是 `-O0` 下的版本（可編輯程式碼不能使用 `__OPTIMIZE__`）。受保護標頭若把自己標為系統標頭（`#pragma GCC system_header`，或旗標為 3 的 linemarker），會像以前一樣隱藏自身的 VLA 警告；受保護標頭中的診斷 pragma 也能關閉之後程式碼的 VLA 警告，此時堆疊框檢查只看得到 `-O0` 下產生的函式中的 VLA。
- `PROVED` 只相對於已批准的 ACSL 契約成立，且以 WP 的記憶體與機器模型為前提。
- 修復政策是縱深防禦（文字規則）；真正的防線是受保護的 harness 加上獨立驗證器。
- C++20 沒有形式化關卡（CBMC 無法解析現代 C++，ESBMC 尚未整合），因此 C++ 的最佳結果是 `UNKNOWN`。
- Lean 證明的是模型，它與 C 程式碼之間只透過差分測試向量連結。
- bubblewrap 不是虛擬機；高風險工作請在獨立的容器或 VM 中執行。

更多文件（繁體中文）：[架構](docs/architecture.md) · [規格格式](docs/spec-format.md) · [狀態語意](docs/status-semantics.md) · [信任模型](docs/trust-model.md) · [安全性](docs/security.md) · [實驗](docs/experiments.md) · [Roadmap](docs/roadmap.md)
