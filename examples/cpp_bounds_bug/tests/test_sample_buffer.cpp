// Trusted test driver (protected) for lc::SampleBuffer.
// Usage: test_sample_buffer <case>; exit status 0 = pass.
#include <cstdio>
#include <cstring>

#include "sample_buffer.hpp"

namespace {

int failures = 0;

void check(bool ok, const char* what, int line) {
    if (!ok) {
        std::printf("FAIL line %d: %s\n", line, what);
        ++failures;
    }
}
#define CHECK(cond) check((cond), #cond, __LINE__)

void test_basic() {
    lc::SampleBuffer b(8);
    CHECK(b.capacity() == 8);
    CHECK(b.size() == 0);
    CHECK(b.at(0) == nullptr);
    CHECK(b.sum() == 0);
    CHECK(b.append(3));
    CHECK(b.append(-1));
    CHECK(b.append(10));
    CHECK(b.size() == 3);
    CHECK(b.at(0) != nullptr && *b.at(0) == 3);
    CHECK(b.at(2) != nullptr && *b.at(2) == 10);
    CHECK(b.at(3) == nullptr);
    CHECK(b.sum() == 12);
}

// Fill to capacity, then one more append must be rejected without touching
// memory beyond the allocation (the storage is exactly capacity ints, so
// AddressSanitizer reports any write past it).
void test_fill_to_capacity() {
    lc::SampleBuffer b(4);
    for (int v = 1; v <= 4; ++v) {
        CHECK(b.append(v * 100));
    }
    CHECK(b.size() == 4);
    CHECK(!b.append(999));
    CHECK(b.size() == 4);
    CHECK(b.sum() == 1000);
    CHECK(b.at(3) != nullptr && *b.at(3) == 400);
}

void test_capacity_one() {
    lc::SampleBuffer b(1);
    CHECK(b.append(7));
    CHECK(!b.append(8));
    CHECK(b.size() == 1 && b.sum() == 7);
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 2) {
        std::printf("usage: %s basic|fill_to_capacity|capacity_one\n", argv[0]);
        return 2;
    }
    if (std::strcmp(argv[1], "basic") == 0) {
        test_basic();
    } else if (std::strcmp(argv[1], "fill_to_capacity") == 0) {
        test_fill_to_capacity();
    } else if (std::strcmp(argv[1], "capacity_one") == 0) {
        test_capacity_one();
    } else {
        std::printf("unknown case %s\n", argv[1]);
        return 2;
    }
    if (failures != 0) {
        std::printf("%d check(s) failed\n", failures);
        return 1;
    }
    std::printf("ok\n");
    return 0;
}
