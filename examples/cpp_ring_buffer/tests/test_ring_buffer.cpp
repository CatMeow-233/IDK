// Trusted test driver (protected): exercises lc::RingBuffer<int, 4> through
// its public interface.  Usage: test_ring_buffer <case>; exit status 0 = pass.
#include <cstdio>
#include <cstring>

#include "ring_buffer.hpp"

namespace {

using Ring = lc::RingBuffer<int, 4>;

static_assert(Ring::capacity() == 4);

int failures = 0;

void check(bool ok, const char* what, int line) {
    if (!ok) {
        std::printf("FAIL line %d: %s\n", line, what);
        ++failures;
    }
}
#define CHECK(cond) check((cond), #cond, __LINE__)

// Compare the buffer with the expected FIFO contents (oldest first).
void expect_contents(const Ring& r, const int* want, std::size_t n, int line) {
    check(r.size() == n, "size matches", line);
    check(r.empty() == (n == 0), "empty() matches", line);
    check(r.full() == (n == Ring::capacity()), "full() matches", line);
    for (std::size_t i = 0; i < n; ++i) {
        const int* p = r.at(i);
        check(p != nullptr && *p == want[i], "element matches", line);
    }
    check(r.at(n) == nullptr, "at(size()) is nullptr", line);
    const int* f = r.front();
    check(n == 0 ? f == nullptr : (f != nullptr && *f == want[0]), "front matches", line);
}

void test_fifo() {
    Ring r;
    expect_contents(r, nullptr, 0, __LINE__);
    CHECK(r.push(1));
    CHECK(r.push(2));
    CHECK(r.push(3));
    const int want[] = {1, 2, 3};
    expect_contents(r, want, 3, __LINE__);
    int out = 0;
    CHECK(r.pop(out) && out == 1);
    CHECK(r.pop(out) && out == 2);
    CHECK(r.pop(out) && out == 3);
    expect_contents(r, nullptr, 0, __LINE__);
}

void test_full() {
    Ring r;
    for (int v = 10; v < 14; ++v) {
        CHECK(r.push(v));
    }
    CHECK(!r.push(99));  // full: rejected, contents unchanged
    const int want[] = {10, 11, 12, 13};
    expect_contents(r, want, 4, __LINE__);
}

void test_empty() {
    Ring r;
    int out = 42;
    CHECK(!r.pop(out));
    CHECK(out == 42);  // unchanged on failure
    CHECK(r.front() == nullptr);
    CHECK(r.push(5));
    CHECK(r.pop(out) && out == 5);
    CHECK(!r.pop(out) && out == 5);
}

// Many push/pop rounds so that head and tail wrap around several times,
// checked against a simple reference model.
void test_wrap_around() {
    Ring r;
    int model[64];
    std::size_t model_head = 0;
    std::size_t model_tail = 0;
    int next = 0;
    for (int round = 0; round < 12; ++round) {
        const int pushes = 1 + round % 4;
        for (int k = 0; k < pushes; ++k) {
            const bool accepted = r.push(next);
            const bool room = (model_tail - model_head) < Ring::capacity();
            CHECK(accepted == room);
            if (room) {
                model[model_tail++ % 64] = next;
            }
            ++next;
        }
        const int pops = 1 + (round * 3) % 4;
        for (int k = 0; k < pops; ++k) {
            int out = -1;
            const bool got = r.pop(out);
            CHECK(got == (model_head != model_tail));
            if (got) {
                CHECK(out == model[model_head++ % 64]);
            }
        }
        int want[4];
        const std::size_t n = model_tail - model_head;
        for (std::size_t i = 0; i < n; ++i) {
            want[i] = model[(model_head + i) % 64];
        }
        expect_contents(r, want, n, __LINE__);
    }
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 2) {
        std::printf("usage: %s fifo|full|empty|wrap_around\n", argv[0]);
        return 2;
    }
    const char* which = argv[1];
    if (std::strcmp(which, "fifo") == 0) {
        test_fifo();
    } else if (std::strcmp(which, "full") == 0) {
        test_full();
    } else if (std::strcmp(which, "empty") == 0) {
        test_empty();
    } else if (std::strcmp(which, "wrap_around") == 0) {
        test_wrap_around();
    } else {
        std::printf("unknown case %s\n", which);
        return 2;
    }
    if (failures != 0) {
        std::printf("%d check(s) failed\n", failures);
        return 1;
    }
    std::printf("ok\n");
    return 0;
}
