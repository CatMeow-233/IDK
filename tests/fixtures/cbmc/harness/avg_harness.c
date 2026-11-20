/* Trusted harness for avg: count is any non-negative int (including 0). */
#include "leanc.h"
#include "cbmc_fx.h"

int main(void)
{
    int total = nondet_int();
    int count = nondet_int();
    LEANC_ASSUME(count >= 0);
    int r = avg(total, count);
    LEANC_REACH("after_call");
    (void)r;
    return 0;
}
