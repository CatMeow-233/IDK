#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "tally.h"

static int count_below(const int *a, int n, int x)
{
    return tally_below(a, n, x);
}

int main(void)
{
    int n = nondet_int();
    LEANC_ASSUME(1 <= n && n <= 1000000);
    int *a = malloc((size_t)n * sizeof(int));
    LEANC_ASSUME(a != NULL);
    int first = count_below(a, n, nondet_int());
    int second = count_below(a, n, nondet_int());
    LEANC_REACH("both_returned");
    assert(0 <= first && first <= n && 0 <= second && second <= n);
    free(a);
    return 0;
}
