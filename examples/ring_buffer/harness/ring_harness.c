/* Trusted CBMC harness for the ring buffer (human-reviewed).
 *
 * Runs a bounded, fully nondeterministic sequence of N_OPS push/pop
 * operations on a ring of nondeterministic capacity 1..CAP_MAX and checks,
 * after every step, the representation invariant, the result of the
 * operation, FIFO order (popped / oldest value) and the observers against a
 * simple linear-array model of the queue.  After the sequence, every stored
 * element is compared via ring_peek and ring_sum (once, to keep CBMC fast).
 */
#include <assert.h>
#include <stdbool.h>

#include "leanc.h"
#include "ring.h"

#define CAP_MAX 3
#define N_OPS 5

int main(void)
{
    int storage[CAP_MAX];
    struct ring r;

    /* Initialisation: invalid arguments are rejected without touching anything. */
    size_t cap = nondet_size_t();
    LEANC_ASSUME(cap <= CAP_MAX || cap > RING_MAX_CAPACITY);  /* storage has cap ints, or cap is rejected */
    int *mem = nondet_bool() ? NULL : storage;
    int rc = ring_init(&r, mem, cap);
    if (mem == NULL) {
        LEANC_REACH("init_null");
        assert(rc == RING_ERR_NULL);
        return 0;
    }
    if (cap == 0 || cap > RING_MAX_CAPACITY) {
        LEANC_REACH("init_bad_capacity");
        assert(rc == RING_ERR_CAPACITY);
        return 0;
    }
    assert(rc == RING_OK);
    assert(ring_init(NULL, storage, cap) == RING_ERR_NULL);
    assert(ring_push(NULL, 1) == RING_ERR_NULL);
    assert(ring_pop(&r, NULL) == RING_ERR_NULL);
    assert(ring_count(NULL) == 0 && ring_is_empty(NULL) && !ring_is_full(NULL) && ring_sum(NULL) == 0);

    /* Model: every value ever pushed, in order; the queue is model[m_head..m_tail). */
    int model[N_OPS];
    size_t m_head = 0;
    size_t m_tail = 0;

    for (size_t step = 0; step < N_OPS; step++) {
        if (nondet_bool()) {
            int v = nondet_int();
            bool was_full = (m_tail - m_head) == cap;
            rc = ring_push(&r, v);
            if (was_full) {
                LEANC_REACH("push_full");
                assert(rc == RING_ERR_FULL);
            } else {
                LEANC_REACH("push_ok");
                assert(rc == RING_OK);
                model[m_tail++] = v;
            }
        } else {
            int out = nondet_int();
            int before = out;
            bool was_empty = m_tail == m_head;
            rc = ring_pop(&r, &out);
            if (was_empty) {
                LEANC_REACH("pop_empty");
                assert(rc == RING_ERR_EMPTY);
                assert(out == before);              /* *out untouched */
            } else {
                LEANC_REACH("pop_ok");
                assert(rc == RING_OK);
                assert(out == model[m_head]);       /* FIFO order */
                m_head++;
            }
        }

        /* Representation invariant. */
        size_t m_count = m_tail - m_head;
        assert(r.buf == storage && r.capacity == cap);
        assert(r.head < r.capacity);
        assert(r.count <= r.capacity);
        assert(r.count == m_count);
        if (r.head + r.count > r.capacity) {
            LEANC_REACH("wrapped");                 /* contents wrap around the end of storage */
        }

        /* Observers agree with the model. */
        assert(ring_count(&r) == m_count);
        assert(ring_is_empty(&r) == (m_count == 0));
        assert(ring_is_full(&r) == (m_count == cap));
        if (m_count > 0) {
            int oldest = 0;
            assert(ring_peek(&r, 0, &oldest) == RING_OK && oldest == model[m_head]);
        }
    }
    LEANC_REACH("after_ops");

    /* Full contents (oldest first) and the sum agree with the model. */
    size_t m_count = m_tail - m_head;
    long long m_sum = 0;
    for (size_t i = 0; i < m_count; i++) {
        int x = 0;
        assert(ring_peek(&r, i, &x) == RING_OK);
        assert(x == model[m_head + i]);
        m_sum += model[m_head + i];
    }
    int y = 0;
    assert(ring_peek(&r, m_count, &y) == RING_ERR_RANGE);
    assert(ring_sum(&r) == m_sum);
    return 0;
}
