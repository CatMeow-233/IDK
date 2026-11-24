#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "ring.h"

void __VERIFIER_assume(int cond);

static void keep(const struct ring *r)
{
    __VERIFIER_assume(r->fill < 16);
}

int main(void)
{
    struct ring *r = malloc(sizeof *r);
    LEANC_ASSUME(r != NULL);
    LEANC_ASSUME(r->fill < 16);
    ring_add(r, nondet_uchar());
    LEANC_REACH("after_add");
    keep(r);
    assert(r->fill < 16);
    (void)ring_peek(r);
    LEANC_REACH("after_peek");
    free(r);
    return 0;
}
