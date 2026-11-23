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

static void check_state(const struct iset *s)
{
    assert(0 <= s->len && s->len <= s->cap && s->cap <= ISET_MAX_LEN);
}

void prove_iset_contains(void)
{
    struct iset s;
    arbitrary_set(&s);
    int r = iset_contains(&s, nondet_int());
    LEANC_REACH("contains_returned");
    assert(r == 0 || r == 1);
    check_state(&s);
    free(s.items);
}

void prove_iset_rank(void)
{
    struct iset s;
    arbitrary_set(&s);
    int r = iset_rank(&s, nondet_int());
    LEANC_REACH("rank_returned");
    assert(0 <= r && r <= s.len);
    check_state(&s);
    free(s.items);
}

void prove_iset_insert(void)
{
    struct iset s;
    arbitrary_set(&s);
    int len = s.len;
    int rc = iset_insert(&s, nondet_int());
    LEANC_REACH("insert_returned");
    assert(rc == ISET_OK || rc == ISET_PRESENT || rc == ISET_ERR_TOO_BIG);
    assert(s.len == len + (rc == ISET_OK));
    check_state(&s);
    free(s.items);
}

void prove_iset_remove(void)
{
    struct iset s;
    arbitrary_set(&s);
    int len = s.len;
    int rc = iset_remove(&s, nondet_int());
    LEANC_REACH("remove_returned");
    assert(rc == ISET_OK || rc == ISET_ABSENT);
    assert(s.len == len - (rc == ISET_OK));
    check_state(&s);
    free(s.items);
}
