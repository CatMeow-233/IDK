#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "tally.h"

unsigned long long nondet_ulonglong(void);

int main(void)
{
    int n = nondet_int();
    LEANC_ASSUME(1 <= n && n <= 1000000);
    int *a = malloc((size_t)n * sizeof(int));
    LEANC_ASSUME(a != NULL);
    unsigned long long wide = nondet_ulonglong();
    int c = tally_below(a, n, nondet_int());
    LEANC_REACH("below_returned");
    assert(0 <= c && c <= n);
    assert(wide != (1ull << 40));
    free(a);
    return 0;
}
