/* Trusted CBMC harness for safe_ratio (human-reviewed). */
#include <assert.h>
#include <limits.h>

#include "leanc.h"
#include "safe_ratio.h"

int main(void)
{
    /* No precondition: num and den range over ALL ints, out may be NULL. */
    int num = nondet_int();
    int den = nondet_int();
    int out_cell = nondet_int();
    int out_before = out_cell;
    int *out = nondet_bool() ? NULL : &out_cell;

    int rc = safe_ratio(num, den, out);
    LEANC_REACH("after_call");

    /* POST5: the return value is always a documented status code. */
    assert(rc == SAFE_RATIO_OK || rc == SAFE_RATIO_ERR_NULL || rc == SAFE_RATIO_ERR_DIV_ZERO ||
           rc == SAFE_RATIO_ERR_OVERFLOW);
    if (out == NULL) {
        LEANC_REACH("null_out");
        assert(rc == SAFE_RATIO_ERR_NULL);                /* POST4 */
    } else if (den == 0) {
        LEANC_REACH("zero_den");
        assert(rc == SAFE_RATIO_ERR_DIV_ZERO);            /* POST2 */
        assert(out_cell == out_before);                   /* *out untouched on error */
    } else if (num == INT_MIN && den == -1) {
        LEANC_REACH("int_min_by_minus_one");
        assert(rc == SAFE_RATIO_ERR_OVERFLOW);            /* POST3 */
        assert(out_cell == out_before);
    } else {
        LEANC_REACH("representable");
        assert(rc == SAFE_RATIO_OK);                      /* POST1 */
        /*
         * Exact quotient, checked without trusting '/': num == q * den + r with
         * |r| < |den| and r == 0 or r has the sign of num (truncation toward zero).
         * 64-bit arithmetic cannot overflow here (|q|, |den| <= 2^31).
         */
        long long q = out_cell;
        long long r = (long long)num - q * (long long)den;
        long long aden = den < 0 ? -(long long)den : (long long)den;
        assert((r < 0 ? -r : r) < aden);
        assert(r == 0 || (r < 0) == (num < 0));
    }
    return 0;
}
