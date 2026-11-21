/* Trusted CBMC harness for clamp_int (human-reviewed). */
#include <assert.h>
#include <stddef.h>

#include "leanc.h"
#include "clamp.h"

int main(void)
{
    int x = nondet_int();   /* all three inputs range over every int */
    int lo = nondet_int();
    int hi = nondet_int();
    int cell = nondet_int();
    int before = cell;
    int *out = nondet_bool() ? NULL : &cell;

    int rc = clamp_int(x, lo, hi, out);
    LEANC_REACH("after_call");

    /* POST4: the return value is one of the documented codes. */
    assert(rc == CLAMP_OK || rc == CLAMP_ERR_NULL || rc == CLAMP_ERR_RANGE);
    if (out == NULL) {
        LEANC_REACH("null_out");
        assert(rc == CLAMP_ERR_NULL);
    } else if (lo > hi) {
        LEANC_REACH("empty_range");
        assert(rc == CLAMP_ERR_RANGE);
        assert(cell == before);            /* *out untouched on error */
    } else {
        assert(rc == CLAMP_OK);
        assert(lo <= cell && cell <= hi);  /* POST1 */
        if (x < lo) {
            LEANC_REACH("below");
            assert(cell == lo);
        } else if (x > hi) {
            LEANC_REACH("above");
            assert(cell == hi);
        } else {
            LEANC_REACH("inside");
            assert(cell == x);             /* POST2: identity inside the interval */
        }
    }
    return 0;
}
