#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "ring.h"

static unsigned ring_room(const struct ring *r)
{
    return r->fill < 16 ? 16u - r->fill : 0u;
}

int main(void)
{
    struct ring *r = malloc(sizeof *r);
    LEANC_ASSUME(r != NULL);
    LEANC_ASSUME(r->fill < 16);
    ring_add(r, nondet_uchar());
    LEANC_REACH("after_add");
    assert(ring_room(r) > 0);
    (void)ring_space(r);
    LEANC_REACH("after_space");
    free(r);
    return 0;
}
