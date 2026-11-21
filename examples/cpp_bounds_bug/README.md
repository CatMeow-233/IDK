# cpp_bounds_bug — C++ 成員函式的差一錯誤（由 AddressSanitizer 抓到）

需求：「以 C++20 實作類別 `lc::SampleBuffer`：建構時在 heap 上配置恰好 `capacity` 個 `int`（RAII，解構時
釋放，不可複製）；`append(sample)` 在已滿時必須回傳 `false` 且不改變內容；`at(i)` 在 `i >= size()` 時回傳
`nullptr`；`sum()` 回傳總和。任何操作都不得越界存取。」

| 檔案 | 角色 | 信任 |
|---|---|---|
| `spec.yaml` | 規格（target `append`；gates：`compile`、`static_analysis`、`sanitizer`） | 批准後鎖定 |
| `include/sample_buffer.hpp` | 介面：`class lc::SampleBuffer`（`explicit` 建構子、解構子、刪除的複製操作、`[[nodiscard]] ... noexcept` 成員） | 受保護 |
| `tests/test_sample_buffer.cpp` | 可信任的 C++ 測試驅動程式：`basic`、`fill_to_capacity`、`capacity_one`（儲存區剛好 capacity 個 int，ASan 可抓越界） | 受保護 |
| `src/sample_buffer.cpp` | 成員函式定義（**含錯誤**） | 可編輯 |
| `mock/generate/src/sample_buffer.cpp` | Mock 生成（與 `src/` 相同，含同一個錯誤） | — |
| `mock/repair/round-1.json` | Mock 修復第 1 輪：把 `size_ > capacity_` 改成 `size_ >= capacity_` | — |

錯誤：`append()` 用 `if (size_ > capacity_) return false;` 檢查是否已滿，因此在 `size_ == capacity_` 時仍會寫入
`data_[capacity_]`，越過 heap 配置的尾端一個 `int`。

```bash
leanc spec approve examples/cpp_bounds_bug/spec.yaml --by alice --yes
leanc verify examples/cpp_bounds_bug/spec.yaml               # => RUNTIME_TEST_FAILED（ASan）
leanc run examples/cpp_bounds_bug/spec.yaml --provider mock  # => UNKNOWN（修復 1 輪並寫回；沒有形式化 gate）
```

* `clang++ -std=c++20` 編譯無警告；C++ 完整性 gate 通過（只匯入標準的 `operator new[]`/`operator delete[]`
  與 `memset`）；Clang Static Analyzer 沒有回報（它不會把 `size_` 與配置大小關聯起來）。
* 可信任測試 `fill_to_capacity` 與 `capacity_one` 在 AddressSanitizer 下失敗：
  `heap-buffer-overflow ... WRITE of size 4 ... src/sample_buffer.cpp:14 in lc::SampleBuffer::append(int)`
  （堆疊中的 C++ 名稱已由 symbolizer 解碼）。整體狀態 `RUNTIME_TEST_FAILED`。
* `leanc run --provider mock`：第 1 輪修復通過 policy 檢查，所有必要 gate 都通過，修復後的原始碼寫回工作區。
  但規格沒有形式化 gate（沒有可用的 C++20 模型檢查器；`cbmc`/`wp` 不支援 C++），所以最終狀態**誠實地維持
  `UNKNOWN`**，而不是 `VERIFIED_BOUNDED`：sanitizer 只觀察到測試執行過的路徑。
