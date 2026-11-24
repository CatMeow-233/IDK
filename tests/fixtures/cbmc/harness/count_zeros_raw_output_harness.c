/* The call and its false assertion run only once raw/cbmc-verify.json exists. */
#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "cbmc_fx.h"

#define MAX_N 8

#if __has_include("../../raw/cbmc-verify.json")
#define ARMED 1
#else
#define ARMED 0
#endif

int main(void)
{
    size_t n = nondet_size_t();
    LEANC_ASSUME(n <= MAX_N);
    int *a = malloc(n * sizeof(int));
    if (ARMED) {
        size_t z = count_zeros(a, n);
        LEANC_REACH("after_call");
        assert(z > n);
    }
    free(a);
    return 0;
}
