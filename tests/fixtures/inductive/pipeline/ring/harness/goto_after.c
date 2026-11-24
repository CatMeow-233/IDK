#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "ring.h"

int main(void)
{
    struct ring *r = malloc(sizeof *r);
    LEANC_ASSUME(r != NULL);
    LEANC_ASSUME(r->fill < 16);
    ring_add(r, nondet_uchar());
    LEANC_REACH("after_add");
    if (r->fill >= 16) {
        goto done;
    }
    assert(r->fill < 16);
    (void)ring_peek(r);
    LEANC_REACH("after_peek");
done:
    free(r);
    return 0;
}
