#include <stdlib.h>

#include "iset.h"

#define ISET_MIN_CAP 8

static int lower_bound(const int *items, int len, int x)
{
    int lo = 0;
    int hi = len;
    while (lo < hi) __CPROVER_assigns(lo, hi) __CPROVER_loop_invariant((0 <= lo) && (lo <= hi) && (hi <= len)) __CPROVER_decreases(hi - lo) {
        int mid = lo + (hi - lo) / 2;
        if (items[mid] < x) {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    return lo;
}

static int found_at(const struct iset *s, int pos, int x)
{
    return pos < s->len && s->items[pos] == x;
}

static int next_cap(int cap)
{
    if (cap < ISET_MIN_CAP) {
        return ISET_MIN_CAP;
    }
    return cap * 2;
}

static int resize(struct iset *s, int new_cap)
{
    int *items = realloc(s->items, (size_t)new_cap * sizeof(int));
    if (items == NULL) {
        return ISET_ERR_NOMEM;
    }
    s->items = items;
    s->cap = new_cap;
    return ISET_OK;
}

int iset_init(struct iset *s)
{
    if (s == NULL) {
        return ISET_ERR_NULL;
    }
    s->items = NULL;
    s->len = 0;
    s->cap = 0;
    return ISET_OK;
}

int iset_reserve(struct iset *s, int want)
{
    if (s == NULL) {
        return ISET_ERR_NULL;
    }
    if (want < 0) {
        return ISET_ERR_RANGE;
    }
    if (want <= s->cap) {
        return ISET_OK;
    }
    return resize(s, want);
}

int iset_insert(struct iset *s, int x)
{
    if (s == NULL) {
        return ISET_ERR_NULL;
    }
    int pos = lower_bound(s->items, s->len, x);
    if (found_at(s, pos, x)) {
        return ISET_PRESENT;
    }
    if (s->len == s->cap) {
        if (s->cap == ISET_MAX_LEN) {
            return ISET_ERR_TOO_BIG;
        }
        int rc = resize(s, next_cap(s->cap));
        if (rc != ISET_OK) {
            return rc;
        }
    }
    for (int k = s->len; k > pos; k--) __CPROVER_assigns(k, __CPROVER_object_upto(s->items, (size_t)s->cap * sizeof(int))) __CPROVER_loop_invariant((pos <= k) && (k <= s->len)) __CPROVER_decreases(k - pos) {
        s->items[k] = s->items[k - 1];
    }
    s->items[pos] = x;
    s->len++;
    return ISET_OK;
}

int iset_remove(struct iset *s, int x)
{
    if (s == NULL) {
        return ISET_ERR_NULL;
    }
    int pos = lower_bound(s->items, s->len, x);
    if (!found_at(s, pos, x)) {
        return ISET_ABSENT;
    }
    for (int k = pos; k + 1 < s->len; k++) __CPROVER_assigns(k, __CPROVER_object_upto(s->items, (size_t)s->cap * sizeof(int))) __CPROVER_loop_invariant((pos <= k) && (k < s->len)) __CPROVER_decreases(s->len - k) {
        s->items[k] = s->items[k + 1];
    }
    s->len--;
    return ISET_OK;
}

int iset_contains(const struct iset *s, int x)
{
    if (s == NULL) {
        return 0;
    }
    return found_at(s, lower_bound(s->items, s->len, x), x);
}

int iset_rank(const struct iset *s, int x)
{
    if (s == NULL) {
        return 0;
    }
    return lower_bound(s->items, s->len, x);
}

int iset_len(const struct iset *s)
{
    return s == NULL ? 0 : s->len;
}

int iset_at(const struct iset *s, int i, int *out)
{
    if (s == NULL || out == NULL) {
        return ISET_ERR_NULL;
    }
    if (i < 0 || i >= s->len) {
        return ISET_ERR_RANGE;
    }
    *out = s->items[i];
    return ISET_OK;
}

void iset_destroy(struct iset *s)
{
    if (s == NULL) {
        return;
    }
    free(s->items);
    s->items = NULL;
    s->len = 0;
    s->cap = 0;
}
