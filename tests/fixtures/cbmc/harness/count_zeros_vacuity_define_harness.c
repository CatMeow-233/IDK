/* The call and its false assertion run only in the vacuity build. */
#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "cbmc_fx.h"

#define MAX_N 8

int main(void)
{
    size_t n = nondet_size_t();
    LEANC_ASSUME(n <= MAX_N);
    int *a = malloc(n * sizeof(int));
#ifndef LEANC_VACUITY_CHECK
    if (n > MAX_N)
#endif
    {
        size_t z = count_zeros(a, n);
        LEANC_REACH("after_call");
        assert(z > n);
    }
    free(a);
    return 0;
}
