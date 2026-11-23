#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "iset.h"

#define PREFIX_INSERTS 1
#define N_OPS 2
#define MODEL_MAX (PREFIX_INSERTS + N_OPS)
#define RESERVE_MAX 12

enum op_kind { OP_INSERT, OP_REMOVE, OP_QUERY, OP_RESERVE, OP_COUNT };

struct model {
    int vals[MODEL_MAX];
    int len;
};

static int model_find(const struct model *m, int x)
{
    for (int k = 0; k < m->len; k++) {
        if (m->vals[k] == x) {
            return k;
        }
    }
    return -1;
}

static int model_below(const struct model *m, int x)
{
    int below = 0;
    for (int k = 0; k < m->len; k++) {
        below += m->vals[k] < x;
    }
    return below;
}

static void check_null_arguments(struct iset *s)
{
    int out = 0;
    assert(iset_init(NULL) == ISET_ERR_NULL);
    assert(iset_reserve(NULL, 1) == ISET_ERR_NULL);
    assert(iset_insert(NULL, 1) == ISET_ERR_NULL);
    assert(iset_remove(NULL, 1) == ISET_ERR_NULL);
    assert(iset_contains(NULL, 1) == 0);
    assert(iset_rank(NULL, 1) == 0);
    assert(iset_len(NULL) == 0);
    assert(iset_at(NULL, 0, &out) == ISET_ERR_NULL);
    assert(iset_at(s, 0, NULL) == ISET_ERR_NULL);
    iset_destroy(NULL);
}

static void check_against_model(const struct iset *s, const struct model *m)
{
    assert(iset_len(s) == m->len);
    assert(s->len >= 0 && s->len <= s->cap);
    for (int i = 0; i < m->len; i++) {
        int got = 0;
        assert(iset_at(s, i, &got) == ISET_OK);
        assert(model_find(m, got) >= 0);
        if (i > 0) {
            assert(s->items[i - 1] < got);
        }
    }
}

static void do_insert(struct iset *s, struct model *m)
{
    int x = nondet_int();
    int had = model_find(m, x) >= 0;
    int rc = iset_insert(s, x);
    if (had) {
        LEANC_REACH("insert_present");
        assert(rc == ISET_PRESENT);
        return;
    }
    LEANC_REACH("insert_new");
    assert(rc == ISET_OK);
    m->vals[m->len++] = x;
}

static void do_remove(struct iset *s, struct model *m)
{
    int x = nondet_int();
    int k = model_find(m, x);
    int rc = iset_remove(s, x);
    if (k < 0) {
        LEANC_REACH("remove_absent");
        assert(rc == ISET_ABSENT);
        return;
    }
    LEANC_REACH("remove_present");
    assert(rc == ISET_OK);
    m->len--;
    m->vals[k] = m->vals[m->len];
}

static void do_query(const struct iset *s, const struct model *m)
{
    int x = nondet_int();
    int member = model_find(m, x) >= 0;
    assert(iset_contains(s, x) == member);
    assert(iset_rank(s, x) == model_below(m, x));
    if (member) {
        LEANC_REACH("query_member");
    } else {
        LEANC_REACH("query_absent");
    }
}

static void do_reserve(struct iset *s, int step)
{
    int cap = s->cap;
    int want = step % 2 == 0 ? -1 : RESERVE_MAX;
    int rc = iset_reserve(s, want);
    if (want < 0) {
        LEANC_REACH("reserve_negative");
        assert(rc == ISET_ERR_RANGE && s->cap == cap);
        return;
    }
    LEANC_REACH("reserve_ok");
    assert(rc == ISET_OK && s->cap >= want && s->cap >= cap);
}

int main(void)
{
    struct iset s;
    assert(iset_init(&s) == ISET_OK);
    assert(s.items == NULL && s.len == 0 && s.cap == 0);
    check_null_arguments(&s);

    struct model m;
    m.len = 0;
    for (int k = 0; k < PREFIX_INSERTS; k++) {
        do_insert(&s, &m);
    }
    for (int step = 0; step < N_OPS; step++) {
        unsigned char op = nondet_uchar();
        LEANC_ASSUME(op < OP_COUNT);
        if (op == OP_INSERT) {
            do_insert(&s, &m);
        } else if (op == OP_REMOVE) {
            do_remove(&s, &m);
        } else if (op == OP_QUERY) {
            do_query(&s, &m);
        } else {
            do_reserve(&s, step);
        }
        check_against_model(&s, &m);
    }
    LEANC_REACH("after_ops");
    iset_destroy(&s);
    assert(s.items == NULL && s.len == 0 && s.cap == 0);
    LEANC_REACH("destroyed");
    return 0;
}
