/* Trusted CBMC harness for midpoint (human-reviewed). */
#include <assert.h>

#include "leanc.h"
#include "midpoint.h"

int main(void)
{
    int a = nondet_int();                  /* no preconditions: every int pair */
    int b = nondet_int();

    int m = midpoint(a, b);
    LEANC_REACH("after_call");

    /* Reference: exact sum in 64 bits, then C division (rounds toward zero). */
    long long exact = ((long long)a + (long long)b) / 2;
    assert((long long)m == exact);         /* POST1 */
    int lo = a < b ? a : b;
    int hi = a < b ? b : a;
    assert(lo <= m && m <= hi);            /* POST2 */

    if ((a >= 0) == (b >= 0)) {
        LEANC_REACH("same_sign");          /* where a + b can overflow */
    } else {
        LEANC_REACH("opposite_sign");
    }
    if (((long long)a + (long long)b) % 2 != 0) {
        LEANC_REACH("odd_sum");            /* rounding matters */
    }
    return 0;
}
