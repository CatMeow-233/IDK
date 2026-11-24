#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "ring.h"

#define PROBE_TEXT_(x) #x
#define PROBE_TEXT(x) PROBE_TEXT_(x)
#define PROBE LEANC_REACH

int main(void)
{
    struct ring *r = malloc(sizeof *r);
    LEANC_ASSUME(r != NULL);
    LEANC_ASSUME(r->fill < 16);
    if (PROBE_TEXT(PROBE("after_add"))[1] != '(') {
        ring_add(r, nondet_uchar());
        LEANC_REACH("after_add");
        assert(r->fill < 16);
        (void)ring_peek(r);
        LEANC_REACH("after_peek");
    }
    free(r);
    return 0;
}
