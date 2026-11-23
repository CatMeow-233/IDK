#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "tally.h"

static void expect_negative(int value)
{
    LEANC_ASSUME(value < 0);
}

int main(void)
{
    int n = nondet_int();
    LEANC_ASSUME(1 <= n && n <= 1000000);
    int *a = malloc((size_t)n * sizeof(int));
    LEANC_ASSUME(a != NULL);
    int c = tally_below(a, n, nondet_int());
    LEANC_REACH("below_returned");
    expect_negative(c);
    assert(c < 0);
    free(a);
    return 0;
}
