/* Trusted CBMC harness for checked_add (human-reviewed). */
#include <assert.h>
#include <limits.h>

#include "leanc.h"
#include "checked_add.h"

int main(void)
{
    /* No precondition: a and b range over ALL ints, out may be NULL. */
    int a = nondet_int();
    int b = nondet_int();
    int out_cell = nondet_int();
    int out_before = out_cell;
    int *out = nondet_bool() ? NULL : &out_cell;

    int rc = checked_add(a, b, out);
    LEANC_REACH("after_call");

    /* The exact mathematical sum; 64-bit arithmetic cannot overflow here. */
    long long exact = (long long)a + (long long)b;

    /* POST4: the return value is always a documented status code. */
    assert(rc == CHECKED_ADD_OK || rc == CHECKED_ADD_ERR_NULL || rc == CHECKED_ADD_ERR_OVERFLOW);
    if (out == NULL) {
        LEANC_REACH("null_out");
        assert(rc == CHECKED_ADD_ERR_NULL);               /* POST3 */
    } else if (exact > INT_MAX) {
        LEANC_REACH("positive_overflow");
        assert(rc == CHECKED_ADD_ERR_OVERFLOW);           /* POST2 */
        assert(out_cell == out_before);                   /* *out untouched on error */
    } else if (exact < INT_MIN) {
        LEANC_REACH("negative_overflow");
        assert(rc == CHECKED_ADD_ERR_OVERFLOW);           /* POST2 */
        assert(out_cell == out_before);
    } else {
        LEANC_REACH("representable");
        assert(rc == CHECKED_ADD_OK);                     /* POST1 */
        assert((long long)out_cell == exact);             /* exact value, full int range */
    }
    return 0;
}
