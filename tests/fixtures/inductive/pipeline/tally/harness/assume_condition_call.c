#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "tally.h"

static int few_below_zero(const int *a, int n)
{
    return tally_below(a, n, 0) < 10;
}

int main(void)
{
    int n = nondet_int();
    LEANC_ASSUME(1 <= n && n <= 1000000);
    int *a = malloc((size_t)n * sizeof(int));
    LEANC_ASSUME(a != NULL);
    LEANC_ASSUME(few_below_zero(a, n));
    LEANC_REACH("narrowed");
    free(a);
    return 0;
}
