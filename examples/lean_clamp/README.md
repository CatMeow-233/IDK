# lean_clamp — Lean 4 模型證明（V2 `lean` 關卡）

需求：「實作一個把整數限制在閉區間 `[lo, hi]` 內的函式；`lo > hi` 必須回傳錯誤且不寫入輸出，
`out == NULL` 必須回傳錯誤。另以 Lean 4 建立數學模型並證明其性質。」

| 檔案 | 角色 | 信任 |
|---|---|---|
| `spec.yaml` | 結構化規格（關卡：compile、static_analysis、sanitizer、cbmc、lean） | 批准後鎖定 |
| `include/clamp.h` | 介面（回傳碼） | 受保護 |
| `harness/clamp_harness.c` | CBMC harness：`x`、`lo`、`hi` 為任意 int，`out` 可為 NULL，6 個 `LEANC_REACH` 探針 | 受保護 |
| `tests/test_clamp.c` | 可信任測試（ASan/UBSan/LSan） | 受保護 |
| `lean/Spec.lean` | **Lean 數學模型** `clamp : Int → Int → Int → Option Int` 與 6 個定理**敘述**（`def … : Prop`） | 受保護 |
| `lean/Vectors.lean` | 由模型計算 1331 組測試向量的 Lean 程式（`lean --run`） | 受保護 |
| `tests/clamp_vectors.c` | 讀取向量、逐一比對 C 實作的差分測試 driver | 受保護 |
| `src/clamp.c` | C 實作（AI 可生成/修復） | 可編輯 |
| `lean/Proofs.lean` | 定理的**證明**（AI 可撰寫/修復）；隨附版本全部是 `sorry` | 可編輯 |
| `mock/` | Mock LLM 固定輸出（非真實 LLM 表現）：`generate/src/clamp.c`（C 程式碼）、`generate/lean/Proofs.lean`（佔位證明，與隨附版本相同）、`repair/round-1.json`、`round-2.json`（Lean 證明） | — |

## 執行

```bash
leanc spec approve examples/lean_clamp/spec.yaml --by alice --yes
leanc verify examples/lean_clamp/spec.yaml                       # => UNKNOWN（定理未證明）
leanc run examples/lean_clamp/spec.yaml --provider mock --regenerate   # => VERIFIED_BOUNDED
```

1. **verify → `UNKNOWN`**：C 程式碼本身正確（所有 C 關卡 PASS，1331 組向量全部一致），但
   `lean/Proofs.lean` 只有 `sorry`，Lean 關卡回報 6 個 `unproved`（依賴 `sorryAx`）→ `INCONCLUSIVE`。
   這種「只差證明」的狀態屬於 *proof-repairable*。
2. **run → `VERIFIED_BOUNDED`**：mock 修復第 1 輪證明 3 個定理（未解決的義務變少 → 接受），
   第 2 輪證明其餘 3 個 → Lean 關卡 PASS。最終狀態是 `VERIFIED_BOUNDED`，**不是 `PROVED`**。

## Lean 關卡檢查了什麼

- 可信任的 spec 模組、可編輯的證明檔都在 sandbox（bwrap，無網路）中以 `lean` 編譯：Lean 的巨集、
  `#eval`、`initialize` 等在編譯時期就會執行程式碼。
- 編譯前先套用 Lean 政策規則（`leanc/repair/lean_policy.py`）：禁止 `axiom`、`sorry`（修復時不得新增）、
  `unsafe`/`opaque`/`partial`、`@[extern]`/`@[implemented_by]`、`native_decide`、`set_option debug.*`、
  `#eval`/`run_cmd`/`initialize`/`macro`/`syntax`/`notation`、`instance`/`export`、
  以及 spec 模組、其他證明模組、`Init`、`Std` 以外的 `import`。
- 產生的可信任檢查器以 `addDecl` 讓 **Lean 核心（kernel）** 檢查「證明常數的型別 ≡ 批准的敘述」
  （不經名稱解析），並收集證明依賴的公理：只接受 `propext`、`Classical.choice`、`Quot.sound`。
  `sorryAx`、`Lean.ofReduceBool`（native_decide）、`Lean.trustCompiler` 或自訂公理 → 未證明。
- `leanchecker` 對證明模組做獨立的 kernel replay（偵測未經 kernel 檢查而進入環境的宣告）。
- 差分測試：`lean/Vectors.lean` 由模型算出期望值，`tests/clamp_vectors.c` 與 C 實作一起以
  sanitizer 旗標建置後在 sandbox 中執行；任何不一致 → `RUNTIME_TEST_FAILED`。

## 為什麼不是 PROVED

Lean 證明的是 **模型**（`lean/Spec.lean` 中的 `clamp`）的性質，不是 C 程式碼。模型與 C 實作之間
只有 1331 組差分測試向量的連結（報告的 *Scope & assumptions* 會寫明）。C 程式碼的形式化結果
仍來自有界的 CBMC，所以最佳狀態是 `VERIFIED_BOUNDED`；只有 Frama-C/WP（`wp` 關卡）能產生 `PROVED`。
