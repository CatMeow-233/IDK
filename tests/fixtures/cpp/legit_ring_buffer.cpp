// Legitimate C++20 implementation using constructs that must NOT be flagged.
#include "ring_buffer.hpp"

#include <algorithm>
#include <cstdint>
#include <utility>

#if __cplusplus >= 202002L && defined(__cpp_concepts)
#define LC_ADVANCE(i) ((i) + 1 == N ? 0 : (i) + 1)
#else
#error "C++20 required"
#endif

namespace lc {
namespace {

constexpr std::uint32_t kSeed = 0x00'00'00'01;  // digit separators
[[maybe_unused]] constexpr const char* kDoc = R"doc(ring "buffer": exit(0); throw;
#ifdef __clang__ (inside a raw string, not a directive)
)doc";

struct Index {  // helper with internal linkage
    std::size_t value;
    [[nodiscard]] constexpr std::size_t wrapped(std::size_t n) const noexcept { return value >= n ? value - n : value; }
};

template <typename F>
constexpr std::size_t apply(F f, std::size_t x) noexcept {
    return f(x);
}

}  // namespace

template <std::copyable T, std::size_t N>
bool RingBuffer<T, N>::push(const T& value) noexcept {
    if (count_ == N) {
        return false;
    }
    static constexpr std::size_t kOne = kSeed;
    const std::size_t tail = apply([this](std::size_t x) { return Index{head_ + x}.wrapped(N); }, count_);
    buf_[tail] = value;
    count_ += kOne;
    return true;
}

template <std::copyable T, std::size_t N>
bool RingBuffer<T, N>::pop(T& out) noexcept {
    if (count_ == 0) {
        return false;
    }
    out = std::as_const(buf_)[head_];
    head_ = LC_ADVANCE(head_);
    --count_;
    return true;
}

template <std::copyable T, std::size_t N>
const T* RingBuffer<T, N>::front() const noexcept {
    return count_ == 0 ? nullptr : &buf_[head_];
}

template <std::copyable T, std::size_t N>
const T* RingBuffer<T, N>::at(std::size_t i) const noexcept {
    if (i >= count_) {
        return nullptr;
    }
    const std::size_t idx = Index{head_ + std::min(i, N - 1)}.wrapped(N);
    return &buf_[idx];
}

template class RingBuffer<int, 4>;

}  // namespace lc
