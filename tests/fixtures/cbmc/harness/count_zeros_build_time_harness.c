/* Branches on the build clock before the call. */
#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "cbmc_fx.h"

#define MAX_N 8

static const char built[] = __DATE__ " " __TIME__;

int main(void)
{
    size_t n = nondet_size_t();
    LEANC_ASSUME(n <= MAX_N);
    int *a = malloc(n * sizeof(int));
    if ((built[sizeof built - 2] - '0') % 2 == 0) {
        free(a);
        return 0;
    }
    size_t z = count_zeros(a, n);
    LEANC_REACH("after_call");
    assert(z > n);
    free(a);
    return 0;
}
