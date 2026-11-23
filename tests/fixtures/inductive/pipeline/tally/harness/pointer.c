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
    int (*count)(const int *, int, int) = tally_below;
    int c = count(a, n, nondet_int());
    LEANC_REACH("below_returned");
    assert(0 <= c && c <= n);
    free(a);
    return 0;
}
