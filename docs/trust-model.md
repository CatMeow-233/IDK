# 信任模型（Trust Model）

> 程式碼是否正確，不能只由生成程式碼的 AI 自己判斷。

Lean C 把所有產物分成三類：

| 類別 | 例子 | 誰可以寫 | 如何保護 |
|---|---|---|---|
| **可信任**（trusted） | 已批准的 `spec.yaml`、CBMC harness、inductive harness、可信測試 driver、介面標頭 | 只有人類（審查後 `leanc spec approve`） | 語意雜湊 + 檔案 SHA-256 + HMAC 簽章；每次驗證前在快照上重新檢查 |
| **不受信任的候選**（untrusted） | AI 規格草案、AI harness 草案、AI 生成/修復的 C 原始碼、迴圈契約檔 | LLM provider（經政策檢查後） | 只能寫入 `sources.editable`（修復時也包含 `lean.proofs` 與 `inductive.contracts`）；內容政策；沙箱執行 |
| **獨立裁判**（judges） | Clang、Clang Static Analyzer、ASan/UBSan/LSan、CBMC（opt-in 的 inductive profile 另用其 goto-cc 與 goto-instrument） | — | Lean C 只解析其輸出，不採信 LLM 的任何「已正確」說法 |

## 批准紀錄

`leanc spec approve <spec> --by <name>` 會：

1. 以 pydantic 驗證規格（未知欄位、重複鍵、路徑穿越、editable/受保護重疊 → 拒絕）；
2. 計算規格的**語意雜湊**（正規化後的 JSON；註解/排版變更不影響，任何欄位變更都會改變）；
3. 計算每個受保護檔（harness、inductive harness、測試 driver、標頭）的 SHA-256；
4. 寫入 `.leanc/approvals/<spec_id>.json`（`current` + `history`），並以存放在**工作區外**的金鑰
   （`$LEANC_HOME/approval.key`；`LEANC_HOME` 預設為 `$XDG_CONFIG_HOME/leanc`，未設定 `XDG_CONFIG_HOME` 時為 `~/.config/leanc`；權限 0600）計算 HMAC-SHA256 簽章。

版本規則：內容與目前已批准內容不同時，`version` 必須嚴格遞增；相同內容可重複批准（冪等）。
`leanc spec status` 會顯示自上次批准以來的欄位層級差異。

## 驗證前檢查（每次 verify）

`check_approval` 在 **快照** 上檢查，任何一項失敗即 `SPEC_UNAPPROVED`：

* 有批准紀錄、`spec_id` 與 `version` 相符；
* 規格語意雜湊相符（規格被改 → 失效）；
* 受保護檔集合相同且每個檔案雜湊相符（harness/測試/標頭被改 → 失效）；
* 簽章金鑰存在、`key_id` 相符、HMAC 驗證通過（偽造或竄改紀錄 → 失效）。

## 為什麼修復代理無法「作弊」

| 作弊手法 | 防線 |
|---|---|
| 修改 harness / 測試 / 標頭 / 規格 / 批准紀錄 | 修復政策 `PATH_NOT_EDITABLE`（只允許 `sources.editable`、`lean.proofs` 與 `inductive.contracts`）；即使繞過，雜湊與簽章檢查也會使結果變成 `SPEC_UNAPPROVED` |
| 刪除失敗的 assert | harness 受保護；原始碼中 `assert(` 數量減少 → `ASSERT_REMOVED` |
| 在原始碼加 `__CPROVER_assume` / `LEANC_ASSUME` / `__builtin_assume` / `__builtin_unreachable` | `CPROVER_BUILTIN` / `ASSUMPTION_INJECTION` |
| 降低驗證強度（關閉 `--signed-overflow-check`、縮小 unwind、縮小輸入範圍） | 檢查類別、unwind、輸入限制都在已批准規格/harness 中；`MANDATORY_CHECKS` 不能被規格關閉；修復代理只能改原始碼 |
| 讓 harness 變成 vacuous（例如讓函式 `exit(0)` / `abort()` 或無窮迴圈） | 完整性關卡拒絕 `exit`/`_exit`/`abort`/`atexit`… 匯入（CBMC 把它們建模為 `assume(false)`，會悄悄刪掉輸入）；探針以 CBMC property 為單位計算，任何一個不可達 → `UNKNOWN`；無窮迴圈 → unwinding assertion 失敗 → `UNKNOWN`；都不算改善 |
| 覆寫 harness 依賴的符號（`__assert_fail` 讓所有 assert 消失、`nondet_*` 縮小輸入空間、`main`、libc 函式） | 完整性關卡：物件檔的外部符號必須是介面標頭宣告的名稱；保留名稱、`nondet_*`、弱符號一律拒絕 |
| 讓不同工具看到不同程式（`#ifdef __clang__`、`__OPTIMIZE__`、`__CPROVER__`、`LEANC_VACUITY_CHECK`、`NDEBUG`） | 完整性關卡拒絕在前處理指令中使用保留識別字/工具巨集 |
| 以巨集組出 `_Pragma(CPROVER check disable ...)` 關閉檢查、`#line`/linemarker 偽裝成系統標頭以隱藏分析器警告、`__VERIFIER_assume` | 完整性關卡（前處理後掃描 + 指令規則） |
| 在建構子中 `atexit(_exit(0))` 偽造測試結果 / 躲過 LSan | 拒絕 `.init_array`/`.ctors` 與 `atexit`/`_exit` 匯入；未通過則不執行測試 |
| 測試程式在執行中竄改快照中的 harness | 沙箱只給測試程式自己的目錄（唯讀）與空白案例目錄；批准在每個形式化關卡（cbmc、inductive、wp、lean）前與結束時於快照上再檢查 |
| 把批准紀錄回滾到被取代的舊版本 | `$LEANC_HOME` 中的簽章帳本記錄每個專案+規格已批准的最高版本 |
| 吞掉錯誤 / 固定回傳值 | harness 的後置條件 assert（例如 `*out == arr[idx]`）+ `TRIVIALIZED_TARGET` 啟發式 |
| 關閉警告 / sanitizer | `WARNING_SUPPRESSION`（`#pragma clang diagnostic`）、`SANITIZER_SUPPRESSION`（`no_sanitize`） |
| 嘗試存取主機 / 網路 | `DANGEROUS_CALL`（system/popen/exec/fork/socket/...）+ 沙箱（無網路、唯讀系統目錄、uid nobody） |
| `#include "/etc/shadow"` 洩漏資料 | `INCLUDE_ESCAPE`；沙箱啟用時（`sandbox: none` 會連同工具一起關閉）編譯器/CBMC 也在沙箱中執行，除唯讀的系統目錄（`/usr` 等）外只看得到明確綁定的路徑：快照、leanc include 目錄、工具自己的安裝目錄、`build/` 與 `raw/` |
| 反覆提交同一個補丁 | 候選雜湊去重（cycle detection），不重新驗證、計入回合 |
| 讓結果變成 UNKNOWN/TIMEOUT 以「消除」反例 | `is_improvement` 規則：不確定結果不算改善，會被還原（例外：沒有形式化關卡時全部必要關卡通過；證明修復中未證明目標嚴格減少且沒有新增） |
| 在只剩 inductive 證明未通過的回合修改 C 程式碼（例如把長度夾小）換取 `VERIFIED_INDUCTIVE` | `CONTRACTS_ONLY_ROUND`：這種回合只能改 `inductive.contracts`（驗證前拒絕）；`INDUCTIVE_CODE_EDIT`：`inductive` 在修復開始時沒有通過時，任何回合中改了 C 程式碼又通過 `inductive` 的候選在驗證後被拒絕，不接受、不寫回 |
| 寫一個工具「證明得了」但其實是假的迴圈契約（有副作用的子句、漏掉寫入的 assigns、沒有 decreases、用巨集藏副作用） | 子句白名單文法在任何工具執行前拒絕副作用與巨集；decreases 必填；契約從不被假設，CBMC 以 DFCC 證明 base、step、assigns、decreases；失敗是 `UNKNOWN` |
| 以可編輯碼捕獲可信的複製模型或 harness 函式（同名 static、巨集命名、`leanc_` 前綴） | inductive 關卡先連結可信單元，連結後檢查符號表：可信名稱綁定自己的定義、沒有 `$link` 重新命名、可編輯碼沒有 `leanc_` 名稱 → `ERROR` |
| 讓迴圈沒有契約，只被有界展開 | DFCC 之後仍沒有契約的迴圈 → `loop_without_contract`，CBMC 不執行 |

## V2：契約、模型與證明的信任邊界

| 產物 | 信任 | 規則 |
|---|---|---|
| ACSL 契約（受保護標頭中目標函式原型前） | 可信（批准、雜湊鎖定） | WP 只對它證明；可編輯檔中目標函式的任何契約都被拒絕（Frama-C 會合併契約，`requires \false` 會讓一切 vacuous） |
| 可編輯 C 中的 ACSL | 不受信任 | 只允許 loop invariant/assigns/variant、assert/check、static helper 契約、非遞迴有本體的 predicate/logic、lemma；`admit`、`axiom`、`ghost`、`inductive`、巨集展開技巧一律拒絕 |
| Lean 模型與定理敘述 | 可信（批准、雜湊鎖定） | 由產生的可信 checker 確認證明常數的型別**正好**是批准的敘述 |
| Lean 證明 | 不受信任（AI 撰寫） | kernel 檢查 + `leanchecker` replay；只接受 `propext`/`Classical.choice`/`Quot.sound`；禁止 `sorry`、`axiom`、`unsafe`、`native_decide`、`set_option`（少數除外）、metaprogramming、新 instance/notation/macro；在沙箱中編譯（Lean 編譯期可以執行程式碼） |
| C++ 原始碼 | 不受信任 | C++ 完整性規則：禁止例外、RTTI、coroutine、全域建構子、`[[noreturn]]`、取代 `operator new/delete`、寫入 `std`、`#include` 前的 `#define`；外部符號必須由受保護標頭宣告 |

## Inductive profile（opt-in）的信任邊界

| 產物 | 信任 | 規則 |
|---|---|---|
| inductive harness（`inductive.harness`） | 可信（批准、雜湊鎖定） | 它就是 inductive 的形式化規格：狀態不變量（`LEANC_ASSUME`）、claimed 呼叫與之後的斷言。結構規則見 [spec-format.md](spec-format.md)，違反是 `ERROR` |
| 迴圈契約檔（`inductive.contracts`） | 不受信任（模型或人的提案） | 白名單子句文法；只有呈現結果插入 `cbmc --preprocess` 輸出；CBMC 證明每個子句，沒有子句被假設；修改它不影響批准 |
| 複製模型 `leanc/data/runtime/inductive_models.c` | 可信（Lean C 自帶） | `memcpy` / `memmove` / `memset` 保留 CBMC 5.95.1 函式庫的前置條件（`memcpy` 不重疊、來源 `r_ok`、目的 `w_ok`），`n > 0` 時以 `__CPROVER_havoc_slice` 讓目的內容任意；havoc 不能拿掉（只斷言的模型會隱藏依內容而定的缺陷） |
| `cbmc`、`goto-cc`、`goto-instrument` | 獨立裁判（版本鎖定） | 三者的版本行都必須恰好有一個 `(cbmc-X.Y.Z)` 標記、彼此相同且屬於 `TESTED_INDUCTIVE_CBMC`（只有 5.95.1），`goto-cc` 必須以 gcc 身分執行；否則 `ERROR` |

### 關卡信任什麼、檢查什麼

* **只用 DFCC（D1）**：`goto-instrument --dfcc main --apply-loop-contracts`。從不使用 `--loop-contracts-file`（它接受有副作用的子句：
  量測到錯誤的通過），也從不使用不帶 `--dfcc` 的 `--apply-loop-contracts`（函式庫的寫入，例如 `memset`、`free`，不會對照 assigns
  檢查：量測到錯誤的通過）。
* **子句只以呈現結果進入工具（D2）**：契約子句由 `leanc/lang/c_expr.py` 解析成資料結構，只有完全加括號的呈現結果被插入
  `cbmc --preprocess` 的輸出；子句中不能有巨集名稱，所以沒有巨集展開的攻擊面。
* **可信單元先連結、再檢查符號表（D7、D19）**：goto-cc 先編譯可信單元（複製模型、harness），再編譯可編輯單元；連結後
  `goto-instrument --show-symbol-table` 讀出每個符號的檔案、file-local 屬性與值。可信單元定義的每個名稱，連結後必須仍綁定該單元的
  定義；不能存在 `<可信名稱>$link<N>`（兩個 static 也一樣）；可編輯檔中不能有 `leanc_` 名稱（前處理後的文字與符號表都檢查）；
  原始檔不能宣告 `__CPROVER_` 名稱；claimed 函式必須定義在可編輯檔中且不是 static；`main` 必須由 harness 定義。違反任何一項都是
  `ERROR`，兩種連結順序下的捕獲都會被抓到。
* **未宣告的函式從不 PASS（D18）**：goto-cc 以 `--verbosity 2` 執行，`function 'f' is not declared` 警告是 `undeclared_function`
  （`INCONCLUSIVE`）。否則未宣告的 `nondet_ulonglong()` 只產生 `int` 範圍的值，2^40 才出現的缺陷會通過（量測）。
* **殘留迴圈在 CBMC 之前停止（D9）**：DFCC 之後，`<builtin-library-*>` 與 harness 以外仍沒有契約的迴圈是 `loop_without_contract`
  （`INCONCLUSIVE`），CBMC 不執行。
* **證據（D10）**：verify 執行是 `cbmc dfcc.goto --no-library <強制檢查> --unwind U --unwinding-assertions
  --no-self-loops-to-assumptions [--object-bits N] --json-ui`，U = max(`verification.unwind`, 最長的 assigns 清單 + 2)。每個契約
  必須對應恰好一個 CBMC 迴圈，並有 `loop_invariant_base`、`loop_invariant_step`、`loop_decreases`、`loop_assigns`、
  `loop_step_unwinding` 五個類別，否則 `ERROR`（harness 走不到的契約是 `contract_rejected`）。CBMC 無法判定的性質、harness 斷言
  沒有出現在 CBMC 的性質清單中（斷言機制被覆寫）是 `ERROR`。未成立的性質是 `INCONCLUSIVE` 加原因代碼；這些失敗的 kind 是
  `unproved`，**從來不是反例**（狀態是任意的，真實執行可能到不了），關卡也從不 `FAIL`。
* **vacuity（D11、G3）**：vacuity 執行把每個探針變成必須失敗（可達）的斷言：harness 的 `LEANC_REACH` 位置、claimed 函式入口的
  `leanc_function:<f>`、帶契約迴圈本體開頭的 `leanc_loop:<file>:<line>`、harness 中每次 claimed 呼叫之後的 `leanc_return:<f>`。
  任何探針沒有到達都是 `vacuous_harness`；Lean C 沒有插入的探針出現在報告中是 `ERROR`。
* **harness 結構（D4、G1）**：每條路徑最多一次 claimed 呼叫、claimed 呼叫之後沒有假設、claimed 函式之前不執行可編輯碼，都由插入
  harness 副本與可編輯函式入口的斷言逐路徑檢查；其餘結構規則在建置時檢查。違反都是 `ERROR`。
* **只用強制檢查（D12）**：`MANDATORY_CHECKS`（`bounds`、`pointer`、`div-by-zero`、`signed-overflow`、`pointer-overflow`、
  `undefined-shift`）。從任意狀態出發，合法的模運算環繞與洩漏沒有意義；`verification.checks` 的額外類別只有有界結果，範圍中逐項
  列出。
* **版本鎖定（D13）**：上述語意只對 CBMC 5.95.1 量測過。真實工具測試鎖定量測到的語意：DFCC 下函式庫寫入對照 assigns、decreases
  必填、非 const static 從任意值開始而 const static 保留初始值、havoc 的效果、有副作用子句被拒絕、static `leanc_` 函式的捕獲、
  claimed 呼叫之後的 sloppy 假設（`tests/test_inductive_soundness_semantics.py`、`tests/test_inductive_soundness_rules.py`）。
* **執行時機**：和 `sanitizer`、`cbmc`、`wp`、`lean` 一樣只在完整性關卡通過後執行；和其他形式化關卡一樣，執行前在快照上重新檢查
  批准。工具在沙箱中執行（沙箱啟用時），只讀快照、只寫 `build/`、`raw/`、`logs/`。
* **修復（D14）**：`inductive` 是證明關卡。只剩證明關卡未通過、且其中包含 `inductive` 的回合只能改契約檔（`CONTRACTS_ONLY_ROUND`）；
  `inductive` 關卡在修復開始時沒有通過時，改了 C 程式碼又通過 `inductive` 的候選在驗證後被拒絕（`INDUCTIVE_CODE_EDIT`），永遠不寫回。

### 攻擊與防線

多數攻擊在 CBMC 5.95.1 上量測過（輸入在 `tests/fixtures/inductive/attack/` 與 `tests/fixtures/inductive/pipeline/`，由 `tests/test_inductive_*` 重播）。

| 攻擊 | 沒有防線時的後果 | 防線 |
|---|---|---|
| 可編輯的 static 函式取可信模型的名稱（或以巨集命名的 static `memcpy`），在可編輯單元先連結時捕獲每個 `memcpy` | 4 GiB 級的堆積溢位得到 `VERIFIED_INDUCTIVE` | 可信單元先連結；連結後的符號表檢查；`leanc_` 名稱拒絕 → `ERROR` |
| harness 在 claimed 呼叫之後才 `LEANC_ASSUME` 不變量 | 保持性檢查變成 vacuous，ring buffer 的越界讀取通過 | claimed 呼叫之後的假設 → `ERROR`（同一函式內靜態檢查 + 逐路徑檢查） |
| 呼叫前縮小狀態（`r->fill = r->fill % 8;`）、提前 `return` / `goto`、`exit` / `abort` 類呼叫剪掉斷言所在的路徑 | 缺陷被隱藏 | `ERROR`（呼叫前寫入規則、`main()` 出口規則、路徑終止名稱規則） |
| 第二個 claimed 函式從第一個的結束狀態開始 | 後者只在縮小過的狀態上被檢查 | 每條路徑最多一次 claimed 呼叫 → `ERROR` |
| 可編輯碼在 claimed 函式之前執行（假設中的 predicate、setup 呼叫、claimed 呼叫的引數） | 可編輯碼能決定 claimed 呼叫的起始狀態 | 可編輯函式入口的插入檢查 → `ERROR` |
| harness 讀取由可編輯碼決定值的 const 物件 | 可編輯碼決定 harness 讀到的值 | `ERROR` |
| 不 claim 目標函式，只 claim 輔助函式 | `iset_insert` 的 `cap * 2` 溢位沒有被檢查 | 規格驗證器要求 `inductive.functions` 包含 `target.function` |
| 修復把長度夾小（`if (len > 0x7FFFFFFFu) len = 0x7FFFFFFFu;`） | 有缺陷的 sponge 通過 inductive 關卡 | `CONTRACTS_ONLY_ROUND`、`INDUCTIVE_CODE_EDIT`；人寫的同樣程式碼仍會通過（缺口） |
| 有副作用的子句、藏在可編輯巨集中的副作用 | `--loop-contracts-file` 接受有副作用的子句 | 白名單文法在任何工具執行前拒絕；只用 DFCC |
| `--apply-loop-contracts` 不帶 `--dfcc` | 函式庫對 assigns 以外物件的寫入沒有被回報 | 只用 `--dfcc main --apply-loop-contracts` |
| 沒有 decreases 的契約 | 不終止的迴圈通過 | decreases 必填 |
| 只斷言、不 havoc 的複製模型 | 依目的內容而定的索引缺陷被隱藏 | 模型必須 havoc 目的（`tests/test_inductive_models.py`） |
| harness 中未宣告的 `nondet_ulonglong()` | 2^40 處的缺陷通過 | goto-cc `--verbosity 2` → `undeclared_function` |
| harness 依 `LEANC_VACUITY_CHECK` 分支、宣告 `__CPROVER_assert` 參數 | vacuity 執行看到的是另一個程式；插入的檢查改呼叫 harness 程式碼 | 兩份前處理結果比對、`__CPROVER_` 宣告規則 → `ERROR` |
| claimed 呼叫之後的 `for (;;) {}` | 被變成假設，路徑被剪掉 | verify 執行使用 `--no-self-loops-to-assumptions`（展開斷言失敗） |

### 已知缺口

* **harness 強度（未偵測）**：`LEANC_ASSUME` 以外的 harness 程式碼也會縮小涵蓋的狀態，而且不列在範圍中：宣告的初始值、最後一個
  `LEANC_ASSUME` 之前的寫入、`main()` 第一次 claimed 呼叫之後的寫入（包括下一次 claimed 呼叫之前的寫入）、`main()` 沒有直接呼叫
  claimed 函式時其中的任何寫入、包住斷言的分支、helper 函式。列出的 `LEANC_ASSUME` 只是上界；結論只和 harness 的斷言一樣強，
  批准者必須審查整個 harness。
* **同一物件內的溢出（D8，未偵測）**：經由指標、從成員陣列溢出到同一物件相鄰欄位或填充位元組的存取（`p->a[i]`、指標運算、
  `q = p->a; q[i]`、複製模型），以及複製到字串常值，不會被回報；有界的 `cbmc` 關卡也一樣（具名物件上的 `s.a[i]` 以成員檢查）。
* **函式庫模型中的迴圈（D20，fail closed）**：`goto-instrument --dfcc` 自己連結 CPROVER 函式庫模型；呼叫 `strlen`、`strcpy`、
  `strncpy`、`strncat` 會產生假的寫入集失敗，這種程式碼永遠無法通過本關卡（不完備，從不是錯誤的通過）。`abs`、`malloc`、`free`
  沒有問題；Lean C 在插裝後的 `main()` 開頭呼叫 `free((void *)0)`，讓 DFCC 一定能連結 `free` 的模型。
* **修復限制的殘留（D14）**：上述修復規則只限制修復代理。人寫的、在大尺寸下放棄正確性的程式碼（例如夾小長度），在只斷言弱性質的
  harness 下仍會得到 `VERIFIED_INDUCTIVE`；能讓 `inductive` 通過的正確程式碼修正也必須由人套用。
* **不是 `PROVED`**：`VERIFIED_INDUCTIVE` 永遠不會被提升為 `PROVED`；`PROVED` 只來自必要且通過的 `wp` 關卡。

## AI 生成的規格與 harness

`leanc spec draft` 產出的檔案一律寫在 `drafts/<id>/`，並標記 `provenance.origin: ai-draft` 與
「UNTRUSTED」檔頭；它們在人工審查並批准之前**不會**被任何驗證採用。批准 AI 草案時，
互動式確認會明確警告。系統不會自動批准任何東西；`scripts/run_experiments.py` 與測試會用
暫時的 `LEANC_HOME` 扮演審查者，批准的是人工撰寫的範例。

## 規格 → 證明義務

YAML 中的自然語言前置/後置條件本身**不會**被 CBMC 證明。轉換機制是：

* 前置條件 → harness 中的 `LEANC_ASSUME(...)`（CBMC `__CPROVER_assume`）；
* 後置條件、frame 條件、回傳狀態 → harness 中的 `assert(...)`；
* 安全性質 → CBMC 檢查類別（`bounds`、`pointer`、`div-by-zero`、`signed-overflow`…）+ sanitizer；
* 輸入範圍 → harness 中的 assume（`verification.input_bounds` 只是給人看的紀錄，會寫進報告的範圍說明）；
* inductive profile：狀態不變量 → inductive harness 中的 `LEANC_ASSUME`；呼叫後的性質 → 其中的 `assert`；迴圈不變量 →
  可編輯的契約檔（只是提案，由 CBMC 證明）。

因此審查 harness 是批准流程中最重要的一步：它就是形式化規格。
