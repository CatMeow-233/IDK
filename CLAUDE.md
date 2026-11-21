# Lean C 開發規範（必須遵守）

## 1. 不寫註釋，用結構表達意圖
- Python 原始碼（`leanc/`、`tests/`、`scripts/`）中不得有任何 `#` 註釋（包括 `# noqa`、`# type: ignore`、`#:`）。
- docstring 最多一行：模組一行摘要；公開類別/函式只在名稱不足以說明時才寫一行；私有 `_函式` 不寫。
- 原本靠註釋說明的內容，改用：具名常數（`ASAN_EXIT_CODE = 86`）、名稱即說明的小函式（`_is_unwinding_assertion()`）、
  `Enum` / `Literal` / frozen `dataclass`、描述性變數名稱。真正需要的設計理由寫在 `docs/`。
- 單一模組超過約 600 行就依職責拆分。

## 2. 嚴格分模組，禁止互相依賴
- 分層（只能依賴下層，見 `docs/architecture.md`）：
  `foundation` → `specs`、`lang` → `rules`、`toolchain`、`reports`、`llm` → `backends`、`repair` → `pipeline` → `cli`。
- 模組依賴圖必須無循環；`leanc` 內部 import 一律寫在模組頂層（禁止在函式內延遲 import 來繞過循環）。
- 共用的東西往**下**層移，絕不新增向上依賴或轉出（re-export）墊片。
- 由 `tests/test_architecture.py` 強制；不得放寬該測試。

## 3. 不寫會掩蓋 bug 的 try/except
- 禁止裸 `except:`、`except BaseException`、`except Exception`。唯一例外：`leanc/cli.py` 的 `main()` 最外層，
  必須印出完整 traceback 並回傳 exit 70。
- 禁止靜默吞掉例外（handler 只有 `pass` / `continue` / `return` / `return None`）；僅允許良性競態：
  `ProcessLookupError`、`FileNotFoundError`、`FileExistsError`、`BlockingIOError`、`BrokenPipeError`。
- 只攔截會發生且有意義處理的**具體**例外；`try` 區塊只包住會拋出的那一個呼叫；能用驗證就不用例外控制流程。
- 非預期的錯誤必須往上拋，絕不能被轉成某個狀態、記到 log 裡就算了，更不能變成「成功」。

## 4. 驗證誠實性（專案核心）
- 沒有工具真正跑過並通過，就不得回報成功；超時、無法解析、略過的關卡、工具錯誤都不是成功。
- `VERIFIED_BOUNDED` 不會被升級為 `PROVED`；`PROVED` 只來自 `wp` 關卡對已批准 ACSL 契約的完整證明。

## 常用指令
- 全部測試：`python -m pytest -q`（約 8 分鐘；需要 clang-18、cbmc、bwrap，V2 另需 Frama-C/WP 與 Lean 4）
- 架構規則：`python -m pytest tests/test_architecture.py -q`
- 可重現實驗：`python scripts/run_experiments.py`
