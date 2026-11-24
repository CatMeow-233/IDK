#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "ring.h"

#define ARM LEANC_REACH

static int armed;

static void set_armed(int c, const char *m)
{
    (void)c;
    (void)m;
    armed = 1;
}

static void arm(void __CPROVER_assert(int, const char *))
{
    ARM("arm");
}

int main(void)
{
    armed = 0;
    struct ring *r = malloc(sizeof *r);
    LEANC_ASSUME(r != NULL);
    LEANC_ASSUME(r->fill < 16);
    arm(set_armed);
    if (armed) {
        ring_add(r, nondet_uchar());
        LEANC_REACH("after_add");
        assert(r->fill < 16);
        (void)ring_peek(r);
        LEANC_REACH("after_peek");
    }
    free(r);
    return 0;
}
