#include "sample_buffer.hpp"

namespace lc {

SampleBuffer::SampleBuffer(std::size_t capacity)
    : data_(new int[capacity]()), size_(0), capacity_(capacity) {}

SampleBuffer::~SampleBuffer() { delete[] data_; }

bool SampleBuffer::append(int sample) noexcept {
    if (size_ > capacity_) {
        return false;
    }
    data_[size_] = sample;
    ++size_;
    return true;
}

const int* SampleBuffer::at(std::size_t i) const noexcept {
    return i < size_ ? &data_[i] : nullptr;
}

long long SampleBuffer::sum() const noexcept {
    long long total = 0;
    for (std::size_t i = 0; i < size_; ++i) {
        total += data_[i];
    }
    return total;
}

}  // namespace lc
