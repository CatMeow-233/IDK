#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "ring.h"

static void noop(int c, const char *m)
{
    (void)c;
    (void)m;
}

static void keep(void (*__CPROVER_assert)(int, const char *), int c)
{
    LEANC_ASSUME(c);
}

int main(void)
{
    struct ring *r = malloc(sizeof *r);
    LEANC_ASSUME(r != NULL);
    LEANC_ASSUME(r->fill < 16);
    ring_add(r, nondet_uchar());
    LEANC_REACH("after_add");
    keep(noop, r->fill < 16);
    assert(r->fill < 16);
    (void)ring_peek(r);
    LEANC_REACH("after_peek");
    free(r);
    return 0;
}
