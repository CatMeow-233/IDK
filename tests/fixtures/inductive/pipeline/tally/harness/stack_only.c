#include <assert.h>

#include "leanc.h"
#include "tally.h"

int main(void)
{
    int a[4];
    int n = nondet_int();
    LEANC_ASSUME(1 <= n && n <= 4);
    int c = tally_below(a, n, nondet_int());
    assert(0 <= c && c <= n);
    LEANC_REACH("end");
    return 0;
}
