# cpp_ring_buffer — C++20 類別模板環形緩衝區（正確程式，無形式化 gate）

需求：「以 C++20 類別模板實作固定容量的先進先出環形緩衝區 `RingBuffer<T, N>`：`push`、`pop`、`front`、
`at(i)`。滿時 `push` 回傳 `false` 且不改變內容；空時 `pop` 回傳 `false` 且不改變輸出參數；任何操作序列
（包含多次繞回）都必須維持 FIFO 順序，且不得越界、不得配置記憶體、不得拋出例外。」

本例示範 Lean C 的 **C++ 支援**（`language: {name: cpp, standard: c++20}`）：

| 檔案 | 角色 | 信任 |
|---|---|---|
| `spec.yaml` | 規格（target `push`；gates 只有 `compile`、`static_analysis`、`sanitizer`） | 批准後鎖定 |
| `include/ring_buffer.hpp` | 介面：`template <std::copyable T, std::size_t N> class lc::RingBuffer`（`constexpr`、`[[nodiscard]]`、`noexcept`、`std::array` 儲存區）與 `extern template class RingBuffer<int, 4>;` | 受保護 |
| `tests/test_ring_buffer.cpp` | 可信任的 C++ 測試驅動程式：`fifo`、`full`、`empty`、`wrap_around`（與參考模型比對 12 輪 push/pop） | 受保護 |
| `src/ring_buffer.cpp` | 成員函式的類別外定義，以及明確實例化 `template class RingBuffer<int, 4>;` | 可編輯 |
| `mock/generate/src/ring_buffer.cpp` | Mock 生成（與 `src/` 相同；非真實 LLM 表現） | — |

```bash
leanc spec approve examples/cpp_ring_buffer/spec.yaml --by alice --yes
leanc verify examples/cpp_ring_buffer/spec.yaml               # => UNKNOWN（所有 gate 通過，但沒有形式化 gate）
leanc run examples/cpp_ring_buffer/spec.yaml --provider mock  # => UNKNOWN（0 輪修復）
```

各 gate 的行為：

* **compile**：以 `clang++ -std=c++20 -Wall -Wextra -Wpedantic` 逐檔編譯（`warnings_policy: block`）。
* **integrity（強制）**：C++ 版本的來源完整性檢查。物件檔中的符號是 mangled 名稱，會先用 `llvm-cxxfilt`
  解碼再判斷：`lc::RingBuffer<int, 4ul>::push(int const&)` 等明確實例化產生的 weak（COMDAT）符號，名稱都由
  受保護標頭宣告，因此允許；`std::array<int, 4ul>::operator[]` 是標準函式庫模板的隱式實例化，也允許
  （前提是來源規則禁止任何寫進 `namespace std` 的程式碼）。
* **static_analysis**：`clang++ --analyze`（含 `cplusplus.*` checkers），無發現。
* **sanitizer**：被測程式碼與可信任 C++ 驅動程式一起以 `clang++` 建置並連結（ASan + UBSan + LSan），4 個測試全部通過。

為什麼是 `UNKNOWN` 而不是 `VERIFIED_BOUNDED`：本機沒有能處理現代 C++ 的模型檢查器（CBMC 的 C++ 前端無法
解析 C++20；Frama-C/WP 只支援 C），規格 schema 也因此拒絕 C++ 規格使用 `cbmc` 與 `wp` gate。靜態分析與
sanitizer 測試只能找到錯誤、不能證明沒有錯誤，所以「所有 gate 通過」的誠實結論就是
`UNKNOWN`（「沒有形式化 gate」）。測試只涵蓋 `RingBuffer<int, 4>` 與測試執行到的路徑。
