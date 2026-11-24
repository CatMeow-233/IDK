#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "ring.h"

void __VERIFIER_assume(int cond);

int main(void)
{
    struct ring *r = malloc(sizeof *r);
    LEANC_ASSUME(r != NULL);
    LEANC_ASSUME(r->fill < 16);
    ring_add(r, nondet_uchar());
    LEANC_REACH("after_add");
    __VERIFIER_assume(r->fill < 16);
    assert(r->fill < 16);
    (void)ring_peek(r);
    LEANC_REACH("after_peek");
    free(r);
    return 0;
}
