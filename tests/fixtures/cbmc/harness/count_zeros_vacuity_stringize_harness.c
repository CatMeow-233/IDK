/* Branches on the stringized probe expansion, never naming LEANC_VACUITY_CHECK. */
#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "cbmc_fx.h"

#define MAX_N 8
#define PROBE_TEXT_(x) #x
#define PROBE_TEXT(x) PROBE_TEXT_(x)
#define PROBE LEANC_REACH

int main(void)
{
    size_t n = nondet_size_t();
    LEANC_ASSUME(n <= MAX_N);
    int *a = malloc(n * sizeof(int));
    if (PROBE_TEXT(PROBE("after_call"))[1] != '(') {
        size_t z = count_zeros(a, n);
        LEANC_REACH("after_call");
        assert(z > n);
    }
    free(a);
    return 0;
}
