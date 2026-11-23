#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "tally.h"

int main(void)
{
    int n = nondet_int();
    LEANC_ASSUME(1 <= n && n <= 1000000);
    int *a = malloc((size_t)n * sizeof(int));
    LEANC_ASSUME(a != NULL);
    tally_clear(a, n);
    int c = tally_below(a, n, nondet_int());
    LEANC_REACH("below_returned");
    assert(0 <= c && c <= n);
    int best = tally_max(a, n);
    LEANC_REACH("max_returned");
    assert(best >= a[0]);
    free(a);
    return 0;
}
