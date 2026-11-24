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
    if (nondet_bool()) {
        int c = tally_below(a, n, nondet_int());
        assert(0 <= c && c <= n);
    }
    LEANC_REACH("end");
    free(a);
    return 0;
}
