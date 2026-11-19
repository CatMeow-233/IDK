# 安全性

Lean C 分析並執行 AI 生成的程式碼，因此把它當成潛在惡意輸入。

| # | 要求 | 實作 |
|---|---|---|
| 1 | 不使用 `shell=True` | `leanc.core.runner.run_command` 只接受 argv 串列（傳字串會 `TypeError`）；`Popen(shell=False)`；NUL 位元組拒絕 |
| 2 | 結構化參數 | 所有工具呼叫都是 argv 陣列；報告中記錄完整 argv |
| 3 | 限制工作目錄 | 工具在 `run_dir/snapshot` 執行，輸出只寫 `build/`、`raw/`、`logs/` |
| 4 | 路徑穿越 | `Workspace.normalize` 拒絕絕對路徑、`..`、反斜線穿越、NUL；規格中的所有路徑在載入時正規化 |
| 5 | 符號連結 | `Workspace.resolve` 拒絕任何路徑元件為 symlink；`O_NOFOLLOW` 讀取；寫入以同目錄暫存檔 + `os.replace`；規格與批准紀錄不得為 symlink |
| 6 | CPU/記憶體/時間/輸出 | `RLIMIT_CPU`、`RLIMIT_AS`（CBMC）、`RLIMIT_FSIZE`、`RLIMIT_NOFILE`、`RLIMIT_CORE=0`；牆鐘逾時後 `killpg(SIGKILL)` 整個 process group；stdout/stderr 增量讀取並設上限，超過即終止並標記 truncated；ASan 改用 `hard_rss_limit_mb`（ASan 需要巨大虛擬位址空間，不能用 `RLIMIT_AS`）；stdin 以非阻塞方式寫入，避免子行程不讀輸入時繞過逾時 |
| 7 | 測試禁止網路 | bubblewrap `--unshare-all`（net/pid/ipc/uts/cgroup/user namespace），沙箱中只有 `lo` |
| 8 | 不修改工作區外檔案 | 所有寫入經 `Workspace`；修復只寫 `sources.editable`；工具（編譯器、CBMC）在沙箱中只能寫 `build/`、`raw/`；**不可信的測試程式**只看得到自己的執行檔目錄（唯讀）與每個案例專屬的空白可寫目錄，看不到快照、`raw/` 或其他關卡的輸出 |
| 9 | 保護規格與 harness | 雜湊鎖定 + HMAC 簽章；修復政策 `PATH_NOT_EDITABLE`；詳見 [trust-model.md](trust-model.md) |
| 10 | 不洩漏憑證 | 子行程使用最小環境（不繼承 `OPENAI_API_KEY` 等）；日誌、報告、LLM transcript、run.json 經 `redact()`（已知環境變數值、`sk-…`、`Authorization: Bearer …`、`api_key=…`、GitHub token） |
| 11 | 補丁政策 | `leanc.repair.policy.check_candidate`：路徑、CPROVER/assume 注入、assert 刪除、警告/sanitizer 抑制、`#include` 逃逸、危險呼叫、inline asm、目標函式被「掏空」、補丁大小 |
| 12 | 隔離執行 | bubblewrap：丟棄所有 capabilities、uid/gid 65534、唯讀 `/usr`、容量上限 256 MB 的私有 `/tmp`、最小 `/dev`、清空環境、`--die-with-parent`；家目錄、`/root`、`/home`、`/opt` 不可見；測試寫入的每個檔案上限 64 MB |
| 13 | 程式完整性 | 強制 `integrity` 關卡：物件檔外部符號白名單（介面標頭）、匯入白名單（CBMC 可建模的 libc）、禁止 constructor/destructor section、禁止依工具而異的前處理、`#pragma`、`#line`、`__VERIFIER_*`；未通過時不執行測試 |
| 14 | 專案設定不能削弱隔離 | `leanc.yaml` 中的 `allow_unsandboxed` 被忽略（並警告）；只有 `--allow-unsandboxed` 旗標能在無沙箱時執行測試 |
| 15 | 符號連結 | `.leanc`、`.leanc/runs`、`.leanc/approvals` 不得為符號連結；`init` 與草案寫入都經 `Workspace`（拒絕 symlink） |

## 沒有沙箱時

`detect_sandbox()` 會實際執行一次 bwrap 探測。若 bwrap 不存在或無法運作（例如容器內沒有 user
namespace），Lean C **不會假裝已啟用沙箱**：

* sanitizer 關卡 `SKIPPED`，原因寫入報告 → 整體 `UNKNOWN`；
* 編譯、靜態分析與 CBMC 仍會執行（它們不執行不受信任的程式），報告註明「no sandbox」；
* 只有明確傳入 `--allow-unsandboxed` 旗標時才會在無沙箱下執行測試（`leanc.yaml` 的同名設定會被忽略並警告），
  stderr 與報告中都會加上警告。**無沙箱時，不可信程式以你的使用者身分執行，可以讀取你能讀的一切**——包括
  leanc 父行程的 `/proc/<pid>/environ`（其中可能有 API key）與 `$LEANC_HOME/approval.key`——並可能修改
  快照。批准在 CBMC 前與結束時會重新檢查，但金鑰外洩後的偽造無法偵測；只應在可拋棄的環境中使用。

## 已知限制

* bubblewrap 不是虛擬機；核心漏洞可能逃逸。高風險環境請在 Docker/VM 中執行（見 `docker/Dockerfile`），
  並維持 bwrap 作為第二層。
* `RLIMIT_CPU` 以行程為單位；`RLIMIT_NPROC` 在 bwrap 的 user namespace 中不可靠，因此行程數沒有硬上限。
  緩解：完整性關卡在物件層級拒絕 `fork`/`vfork`/`clone`/`posix_spawn`/`exec*`/`system`/`daemon` 匯入
  （函式指標取址也會產生匯入），未通過則不執行；整個沙箱受牆鐘逾時與 `--die-with-parent` 約束。
* 每個測試案例目錄在磁碟上；單一檔案上限 64 MB，但檔案數沒有上限（逾時前可寫入的量受時間限制）。
* 政策檢查是靜態文字規則（已去除註解與字串），可能被極端的巨集技巧繞過；真正的正確性防線是受保護的
  harness 與獨立驗證器，政策只是縱深防禦。
