#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "tally.h"

int main(void);

static int again(void)
{
    return main();
}

int main(void)
{
    int n = nondet_int();
    LEANC_ASSUME(1 <= n && n <= 1000000);
    int *a = malloc((size_t)n * sizeof(int));
    LEANC_ASSUME(a != NULL);
    int c = tally_below(a, n, nondet_int());
    LEANC_REACH("below_returned");
    assert(0 <= c && c <= n);
    if (nondet_int()) {
        again();
    }
    free(a);
    return 0;
}
