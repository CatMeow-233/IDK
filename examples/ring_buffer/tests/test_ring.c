/* Trusted runtime tests for the ring buffer (run under ASan/UBSan/LSan). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ring.h"

static int check(int cond, const char *what)
{
    if (!cond) {
        fprintf(stderr, "FAILED: %s\n", what);
        return 1;
    }
    return 0;
}

static int test_basic_fifo(void)
{
    int storage[4];
    struct ring r;
    int out = 0;
    int f = 0;
    f |= check(ring_init(&r, storage, 4) == RING_OK, "init");
    f |= check(ring_push(&r, 10) == RING_OK && ring_push(&r, 20) == RING_OK && ring_push(&r, 30) == RING_OK, "push x3");
    f |= check(ring_pop(&r, &out) == RING_OK && out == 10, "pop 10");
    f |= check(ring_pop(&r, &out) == RING_OK && out == 20, "pop 20");
    f |= check(ring_pop(&r, &out) == RING_OK && out == 30, "pop 30");
    f |= check(ring_is_empty(&r), "empty again");
    return f;
}

static int test_full_empty(void)
{
    /* Heap storage of exactly `capacity` ints so ASan catches any overrun. */
    int *storage = malloc(3 * sizeof *storage);
    struct ring r;
    int out = 77;
    int f = check(storage != NULL, "malloc");
    if (storage == NULL) {
        return f;
    }
    f |= check(ring_init(&r, storage, 3) == RING_OK, "init");
    f |= check(ring_pop(&r, &out) == RING_ERR_EMPTY && out == 77, "pop on empty leaves *out");
    for (int k = 0; k < 3; k++) {
        f |= check(ring_push(&r, k) == RING_OK, "fill");
    }
    f |= check(ring_is_full(&r) && ring_count(&r) == 3, "full");
    f |= check(ring_push(&r, 99) == RING_ERR_FULL, "push on full rejected");
    f |= check(ring_peek(&r, 2, &out) == RING_OK && out == 2, "newest unchanged after rejected push");
    free(storage);
    return f;
}

static int test_wraparound(void)
{
    int *storage = malloc(3 * sizeof *storage);
    struct ring r;
    int out = 0;
    int next_in = 0;
    int next_out = 0;
    int f = check(storage != NULL, "malloc");
    if (storage == NULL) {
        return f;
    }
    f |= check(ring_init(&r, storage, 3) == RING_OK, "init");
    /* Many interleaved operations: head and tail wrap around several times. */
    for (int round = 0; round < 10; round++) {
        f |= check(ring_push(&r, next_in++) == RING_OK, "push a");
        f |= check(ring_push(&r, next_in++) == RING_OK, "push b");
        f |= check(ring_pop(&r, &out) == RING_OK && out == next_out++, "pop in FIFO order");
        f |= check(ring_pop(&r, &out) == RING_OK && out == next_out++, "pop in FIFO order");
    }
    f |= check(ring_push(&r, 100) == RING_OK && ring_push(&r, 101) == RING_OK && ring_push(&r, 102) == RING_OK, "refill");
    f |= check(r.head != 0, "contents wrap (head != 0)");
    for (int k = 0; k < 3; k++) {
        f |= check(ring_peek(&r, (size_t)k, &out) == RING_OK && out == 100 + k, "peek across the wrap");
    }
    f |= check(ring_sum(&r) == 303, "sum across the wrap");
    free(storage);
    return f;
}

static int test_stats(void)
{
    int storage[5];
    struct ring r;
    int out = 0;
    int f = 0;
    f |= check(ring_init(&r, storage, 5) == RING_OK, "init");
    f |= check(ring_count(&r) == 0 && ring_sum(&r) == 0 && ring_is_empty(&r) && !ring_is_full(&r), "empty stats");
    f |= check(ring_push(&r, 2147483647) == RING_OK && ring_push(&r, 2147483647) == RING_OK, "push INT_MAX x2");
    f |= check(ring_sum(&r) == 2LL * 2147483647, "sum does not overflow int");
    f |= check(ring_peek(&r, 2, &out) == RING_ERR_RANGE, "peek past count");
    return f;
}

static int test_init_errors(void)
{
    int storage[2];
    struct ring r;
    int f = 0;
    f |= check(ring_init(&r, storage, 0) == RING_ERR_CAPACITY, "capacity 0");
    f |= check(ring_init(&r, storage, RING_MAX_CAPACITY + 1) == RING_ERR_CAPACITY, "capacity too large");
    f |= check(ring_init(&r, NULL, 2) == RING_ERR_NULL, "NULL storage");
    f |= check(ring_init(NULL, storage, 2) == RING_ERR_NULL, "NULL ring");
    return f;
}

static int test_null_args(void)
{
    int storage[2];
    struct ring r;
    int out = 0;
    int f = 0;
    f |= check(ring_init(&r, storage, 2) == RING_OK, "init");
    f |= check(ring_push(NULL, 1) == RING_ERR_NULL, "push NULL ring");
    f |= check(ring_pop(NULL, &out) == RING_ERR_NULL, "pop NULL ring");
    f |= check(ring_pop(&r, NULL) == RING_ERR_NULL, "pop NULL out");
    f |= check(ring_peek(NULL, 0, &out) == RING_ERR_NULL, "peek NULL ring");
    f |= check(ring_count(NULL) == 0 && ring_is_empty(NULL) && !ring_is_full(NULL) && ring_sum(NULL) == 0, "NULL observers");
    return f;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <case>\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "basic_fifo") == 0) return test_basic_fifo();
    if (strcmp(argv[1], "full_empty") == 0) return test_full_empty();
    if (strcmp(argv[1], "wraparound") == 0) return test_wraparound();
    if (strcmp(argv[1], "stats") == 0) return test_stats();
    if (strcmp(argv[1], "init_errors") == 0) return test_init_errors();
    if (strcmp(argv[1], "null_args") == 0) return test_null_args();
    fprintf(stderr, "unknown case %s\n", argv[1]);
    return 2;
}
