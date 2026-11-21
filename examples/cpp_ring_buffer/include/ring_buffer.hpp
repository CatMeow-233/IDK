// Protected interface (part of the approved spec): a fixed-capacity FIFO ring
// buffer as a C++20 class template.  The member functions are declared here
// and implemented in src/ring_buffer.cpp, which also provides the explicit
// instantiation RingBuffer<int, 4> used by the trusted tests.
#pragma once

#include <array>
#include <concepts>
#include <cstddef>

namespace lc {

template <std::copyable T, std::size_t N>
class RingBuffer {
    static_assert(N > 0, "capacity must be positive");

public:
    constexpr RingBuffer() noexcept = default;

    // Append value as the newest element.  Returns false (and leaves the
    // buffer unchanged) when the buffer already holds N elements.
    [[nodiscard]] bool push(const T& value) noexcept;

    // Remove the oldest element into out.  Returns false (out unchanged)
    // when the buffer is empty.
    [[nodiscard]] bool pop(T& out) noexcept;

    // The oldest element, or nullptr when the buffer is empty.
    [[nodiscard]] const T* front() const noexcept;

    // The i-th oldest element (0 = oldest), or nullptr when i >= size().
    [[nodiscard]] const T* at(std::size_t i) const noexcept;

    [[nodiscard]] constexpr std::size_t size() const noexcept { return count_; }
    [[nodiscard]] constexpr bool empty() const noexcept { return count_ == 0; }
    [[nodiscard]] constexpr bool full() const noexcept { return count_ == N; }
    [[nodiscard]] static constexpr std::size_t capacity() noexcept { return N; }

private:
    // Invariant: head_ < N, count_ <= N; the elements are
    // buf_[(head_ + k) % N] for k = 0 .. count_-1, oldest first.
    std::array<T, N> buf_{};
    std::size_t head_ = 0;
    std::size_t count_ = 0;
};

// Defined (explicitly instantiated) in src/ring_buffer.cpp.
extern template class RingBuffer<int, 4>;

}  // namespace lc
