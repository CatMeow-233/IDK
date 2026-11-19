#include "ring.h"

/* Physical index of the i-th oldest element (i < count <= capacity). */
static size_t slot(const struct ring *r, size_t i)
{
    size_t idx = r->head + i;
    if (idx >= r->capacity) {
        idx -= r->capacity;
    }
    return idx;
}

size_t ring_count(const struct ring *r)
{
    return r == NULL ? 0 : r->count;
}

bool ring_is_empty(const struct ring *r)
{
    return r == NULL || r->count == 0;
}

bool ring_is_full(const struct ring *r)
{
    return r != NULL && r->count == r->capacity;
}

int ring_peek(const struct ring *r, size_t i, int *out)
{
    if (r == NULL || out == NULL) {
        return RING_ERR_NULL;
    }
    if (i >= r->count) {
        return RING_ERR_RANGE;
    }
    *out = r->buf[slot(r, i)];
    return RING_OK;
}

long long ring_sum(const struct ring *r)
{
    long long total = 0;

    if (r == NULL) {
        return 0;
    }
    for (size_t i = 0; i < r->count; i++) {
        total += r->buf[slot(r, i)];
    }
    return total;
}
