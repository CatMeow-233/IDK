#include "ring_buffer.hpp"

namespace lc {

template <std::copyable T, std::size_t N>
bool RingBuffer<T, N>::push(const T& value) noexcept {
    if (count_ == N) {
        return false;
    }
    std::size_t tail = head_ + count_;
    if (tail >= N) {
        tail -= N;
    }
    buf_[tail] = value;
    ++count_;
    return true;
}

template <std::copyable T, std::size_t N>
bool RingBuffer<T, N>::pop(T& out) noexcept {
    if (count_ == 0) {
        return false;
    }
    out = buf_[head_];
    head_ = (head_ + 1 == N) ? 0 : head_ + 1;
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
    std::size_t idx = head_ + i;
    if (idx >= N) {
        idx -= N;
    }
    return &buf_[idx];
}

// The instantiation declared (extern) by the protected header.
template class RingBuffer<int, 4>;

}  // namespace lc
