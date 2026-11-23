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
    int pick = nondet_int();
    if (pick == 0) {
        int c = tally_below(a, n, nondet_int());
        assert(0 <= c && c <= n);
    } else if (pick == 1) {
        int best = tally_max(a, n);
        assert(best >= a[0]);
    } else {
        tally_clear(a, n);
        assert(n >= 1);
    }
    free(a);
    return 0;
}
