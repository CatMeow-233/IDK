#ifndef RING_H
#define RING_H

#include <stdbool.h>
#include <stddef.h>

/* Status codes. */
#define RING_OK 0
#define RING_ERR_NULL (-1)      /* NULL ring / storage / out pointer */
#define RING_ERR_CAPACITY (-2)  /* capacity == 0 or > RING_MAX_CAPACITY */
#define RING_ERR_FULL (-3)      /* push on a full ring */
#define RING_ERR_EMPTY (-4)     /* pop on an empty ring */
#define RING_ERR_RANGE (-5)     /* peek index >= count */

/* Keeps head + count far from SIZE_MAX and the sum of all elements within long long. */
#define RING_MAX_CAPACITY ((size_t)1 << 16)

/*
 * Fixed-capacity FIFO of ints over caller-provided storage.
 * Invariant after a successful ring_init: buf != NULL, 1 <= capacity <= RING_MAX_CAPACITY,
 * head < capacity, count <= capacity; the elements, oldest first, are
 * buf[(head + i) % capacity] for i in [0, count).
 */
struct ring {
    int *buf;
    size_t capacity;
    size_t head;
    size_t count;
};

/* --- src/ring.c ------------------------------------------------------- */
/* Initialise r over storage[0..capacity); the ring starts empty. */
int ring_init(struct ring *r, int *storage, size_t capacity);
/* Append value as the newest element; RING_ERR_FULL (ring unchanged) when full. */
int ring_push(struct ring *r, int value);
/* Remove the oldest element into *out; RING_ERR_EMPTY (*out untouched) when empty. */
int ring_pop(struct ring *r, int *out);

/* --- src/ring_stats.c ------------------------------------------------- */
/* Number of stored elements (0 for NULL). */
size_t ring_count(const struct ring *r);
/* true iff r is NULL or holds no element. */
bool ring_is_empty(const struct ring *r);
/* true iff r is non-NULL and holds capacity elements. */
bool ring_is_full(const struct ring *r);
/* *out = i-th oldest element (i == 0 is the next one ring_pop returns). */
int ring_peek(const struct ring *r, size_t i, int *out);
/* Sum of all stored elements (0 for NULL or empty). */
long long ring_sum(const struct ring *r);

#endif
