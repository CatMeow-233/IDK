#include <limits.h>
#include <stdlib.h>
int nondet_int(void);
int g[4];

static int lower_bound(const int *items, int len, int x)
{
    int lo = 0;
    int hi = len;
    while (lo < hi) __CPROVER_assigns(lo, hi) __CPROVER_loop_invariant(0 <= lo && lo <= hi && hi <= len && abs(lo) >= 0) __CPROVER_decreases(hi - lo)
    {
        int mid = lo + (hi - lo) / 2;
        if (items[mid] < x) { lo = mid + 1; } else { hi = mid; }
    }
    return lo;
}
void prove(void)
{
    int len = nondet_int();
    __CPROVER_assume(len >= 0);
    int *items = malloc((size_t)len * sizeof(int));
    __CPROVER_assume(items != NULL);
    g[0] = 1;
    int r = lower_bound(items, len, nondet_int());
    __CPROVER_assert(0 <= r && r <= len, "result in range");
    __CPROVER_assert(g[0] == 1, "global untouched");
#ifdef VAC
    __CPROVER_assert(0, "leanc_reach:after");
#endif
    free(items);
}
