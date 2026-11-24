#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "ring.h"

static const unsigned *const limit_of_ring = &ring_limit;

int main(void)
{
    struct ring *r = malloc(sizeof *r);
    LEANC_ASSUME(r != NULL);
    LEANC_ASSUME(r->fill < *limit_of_ring);
    ring_add(r, nondet_uchar());
    LEANC_REACH("after_add");
    assert(r->fill < 16);
    (void)ring_peek(r);
    LEANC_REACH("after_peek");
    free(r);
    return 0;
}
