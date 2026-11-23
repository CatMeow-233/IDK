# cbmc_timeout — CBMC 逾時

需求：「實作一個帶溢位檢查的 64 位元無號整數乘法：乘積放得進 uint64_t 時回傳成功並輸出乘積，否則回傳溢位錯誤且不修改輸出。」

`src/mul_checked.c` 是**正確的**。Harness 以兩個完全不受限的 `uint64_t` 為輸入，並以精確的 128 位元乘積
（`unsigned __int128`）作為參考。要證明實作中的 64 位元除法 `UINT64_MAX / a` 與 128 位元乘法一致，
對 SAT 求解器而言極為困難（乘法／除法電路的等價性）。

| Gate | 結果 |
|---|---|
| compile | 通過（無警告） |
| Clang Static Analyzer | 無發現 |
| ASan/UBSan/LSan 測試（normal, boundary, overflow, null） | 全部通過 |
| CBMC（`verification.timeout_s: 3`） | **逾時** |

實測（CBMC 5.95.1，本機）：以與 Lean C 相同的檢查與展開旗標執行
`timeout 25 cbmc src/mul_checked.c harness/mul_checked_harness.c -Iinclude -I<leanc include> --bounds-check --pointer-check --div-by-zero-check --signed-overflow-check --pointer-overflow-check --undefined-shift-check --unsigned-overflow-check --unwind 1 --unwinding-assertions`
在 25 秒後被終止，沒有結論（exit 124）；不設上限的同一執行在 SAT 求解階段（52 852 個變數、282 953 個子句）跑了 269 秒仍未結束（手動終止）。因此 3 秒的上限（即使乘上數倍的 `timeout_scale`）一定會觸發。

預期：`verify` → `TIMEOUT`（exit 21）；`run --provider mock` → `TIMEOUT`，修復迴圈不會被呼叫 ——
逾時不是程式碼缺陷，「測試全過」也不會被升級成驗證結果。要得到結論需要人類決定：例如收窄輸入範圍
（新的、重新批准的規格）或改用更適合的證明工具（V2：Frama-C/WP、Lean 4）。

| 檔案 | 角色 | 信任 |
|---|---|---|
| `spec.yaml` | 規格（`timeout_s: 3`） | 批准後鎖定 |
| `include/mul_checked.h` | 介面 | 受保護 |
| `harness/mul_checked_harness.c` | CBMC harness：不受限的 64 位元輸入、128 位元參考乘積、探針 | 受保護 |
| `tests/test_mul_checked.c` | 可信任測試（含 `(2^32-1)(2^32+1) = 2^64-1` 等邊界） | 受保護 |
| `src/mul_checked.c` | 實作（正確） | 可編輯 |
| `mock/generate/` | Mock 生成（與 `src/` 相同；非真實 LLM 表現） | — |

```bash
leanc spec approve examples/cbmc_timeout/spec.yaml --by alice --yes
leanc verify examples/cbmc_timeout/spec.yaml               # => TIMEOUT (exit 21)
leanc run examples/cbmc_timeout/spec.yaml --provider mock  # => TIMEOUT，0 輪修復
```
