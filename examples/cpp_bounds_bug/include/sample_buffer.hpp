// Protected interface (part of the approved spec): a heap-backed buffer of
// int samples with a capacity fixed at construction (C++20, RAII).
#pragma once

#include <cstddef>

namespace lc {

class SampleBuffer {
public:
    // Allocates storage for exactly `capacity` samples (capacity >= 1).
    explicit SampleBuffer(std::size_t capacity);
    ~SampleBuffer();

    SampleBuffer(const SampleBuffer&) = delete;
    SampleBuffer& operator=(const SampleBuffer&) = delete;

    // Append a sample.  Returns false (buffer unchanged) when it already
    // holds capacity() samples.
    [[nodiscard]] bool append(int sample) noexcept;

    // The i-th sample (0 = first appended), or nullptr when i >= size().
    [[nodiscard]] const int* at(std::size_t i) const noexcept;

    // Sum of all samples (as long long: cannot overflow for size() <= 2^32).
    [[nodiscard]] long long sum() const noexcept;

    [[nodiscard]] std::size_t size() const noexcept { return size_; }
    [[nodiscard]] std::size_t capacity() const noexcept { return capacity_; }

private:
    int* data_;            // exactly capacity_ ints, owned
    std::size_t size_;     // 0 <= size_ <= capacity_
    std::size_t capacity_;
};

}  // namespace lc
