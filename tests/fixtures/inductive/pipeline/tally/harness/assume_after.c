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
    int c = tally_below(a, n, nondet_int());
    LEANC_ASSUME(c < 10);
    LEANC_REACH("below_returned");
    assert(c < 10);
    free(a);
    return 0;
}
