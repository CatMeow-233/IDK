/* A probe spelled through an alias in a static function that is never called. */
#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "cbmc_fx.h"

#define MAX_N 8
#define ARM LEANC_REACH

static void never(void)
{
    ARM("dead");
}

int main(void)
{
    size_t n = nondet_size_t();
    LEANC_ASSUME(n <= MAX_N);
    int *a = malloc(n * sizeof(int));
    size_t z = count_zeros(a, n);
    LEANC_REACH("after_call");
    assert(z <= n);
    free(a);
    return 0;
}
