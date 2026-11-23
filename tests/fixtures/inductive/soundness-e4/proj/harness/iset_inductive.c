#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "iset.h"

static void arbitrary_set(struct iset *s)
{
    s->len = nondet_int();
    s->cap = nondet_int();
    LEANC_ASSUME(0 <= s->len && s->len <= s->cap && s->cap <= ISET_MAX_LEN);
    s->items = malloc((size_t)s->cap * sizeof(int));
    LEANC_ASSUME(s->items != NULL);
}

void prove_iset_contains(void)
{
    struct iset s;
    arbitrary_set(&s);
    int *items = s.items;
    int len = s.len;
    int cap = s.cap;
    int r = iset_contains(&s, nondet_int());
    LEANC_REACH("contains_returned");
    assert(r == 0 || r == 1);
    assert(s.items == items && s.len == len && s.cap == cap);
    free(s.items);
}

void prove_iset_rank(void)
{
    struct iset s;
    arbitrary_set(&s);
    int len = s.len;
    int r = iset_rank(&s, nondet_int());
    LEANC_REACH("rank_returned");
    assert(0 <= r && r <= len);
    assert(0 <= s.len && s.len <= s.cap);
    free(s.items);
}
