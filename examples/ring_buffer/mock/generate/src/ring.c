#include "ring.h"

int ring_init(struct ring *r, int *storage, size_t capacity)
{
    if (r == NULL || storage == NULL) {
        return RING_ERR_NULL;
    }
    if (capacity == 0 || capacity > RING_MAX_CAPACITY) {
        return RING_ERR_CAPACITY;
    }
    r->buf = storage;
    r->capacity = capacity;
    r->head = 0;
    r->count = 0;
    return RING_OK;
}

int ring_push(struct ring *r, int value)
{
    size_t tail;

    if (r == NULL) {
        return RING_ERR_NULL;
    }
    if (r->count == r->capacity) {
        return RING_ERR_FULL;
    }
    /* head < capacity and count < capacity, so one subtraction wraps it. */
    tail = r->head + r->count;
    if (tail >= r->capacity) {
        tail -= r->capacity;
    }
    r->buf[tail] = value;
    r->count++;
    return RING_OK;
}

int ring_pop(struct ring *r, int *out)
{
    if (r == NULL || out == NULL) {
        return RING_ERR_NULL;
    }
    if (r->count == 0) {
        return RING_ERR_EMPTY;
    }
    *out = r->buf[r->head];
    r->head++;
    if (r->head == r->capacity) {
        r->head = 0;
    }
    r->count--;
    return RING_OK;
}
