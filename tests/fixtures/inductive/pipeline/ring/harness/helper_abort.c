#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "ring.h"

static void stop_if_full(const struct ring *r)
{
    if (r->fill >= 16) {
        abort();
    }
}

int main(void)
{
    struct ring *r = malloc(sizeof *r);
    LEANC_ASSUME(r != NULL);
    LEANC_ASSUME(r->fill < 16);
    ring_add(r, nondet_uchar());
    LEANC_REACH("after_add");
    stop_if_full(r);
    assert(r->fill < 16);
    (void)ring_peek(r);
    LEANC_REACH("after_peek");
    free(r);
    return 0;
}
