/* Trusted CBMC harness for safe_ratio (human-reviewed). */
#include <assert.h>
#include <limits.h>

#include "leanc.h"
#include "safe_ratio.h"

/* |v| in 64 bits (defined for INT_MIN too). */
static long long mag(int v)
{
    return v < 0 ? -(long long)v : (long long)v;
}

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
         * POST1 value, over the FULL int range: truncated division is
         * characterised by sign and magnitude (no multiplication needed):
         * |q| <= |num|, q == 0 exactly when |num| < |den|, and a non-zero q
         * is negative exactly when num and den have opposite signs.
         */
        int q = out_cell;
        assert(mag(q) <= mag(num));
        assert((q == 0) == (mag(num) < mag(den)));
        assert(q == 0 || ((q < 0) == ((num < 0) != (den < 0))));
        /*
         * POST1 exact value.  Proving "q == num / den" for all 2^64 input pairs
         * means proving two 32-bit dividers equivalent, which is intractable
         * for CBMC's SAT back end; the exact quotient is therefore checked for
         * den == +-1 over the full num range and for every pair in the window
         * |num| <= 256, |den| <= 16 (the trusted tests cover INT_MIN/INT_MAX).
         */
        if (den == 1 || den == -1) {
            LEANC_REACH("unit_den");
            assert(q == num / den);
        }
        if (num >= -256 && num <= 256 && den >= -16 && den <= 16) {
            LEANC_REACH("exact_window");
            assert(q == num / den);
        }
    }
    return 0;
}
