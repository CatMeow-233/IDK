/* Trusted CBMC harness for mul_u64_checked (human-reviewed).
 *
 * The inputs are two completely unconstrained 64-bit values and the
 * reference is the exact 128-bit product.  Proving the 64-bit division in the
 * implementation equivalent to a 128-bit multiplication is a very hard SAT
 * problem: CBMC does not finish within minutes, so with the spec's
 * verification.timeout_s the result is TIMEOUT -- which is reported as such,
 * never as success.
 */
#include <assert.h>

#include "leanc.h"
#include "mul_checked.h"

int main(void)
{
    uint64_t a = nondet_uint64_t();        /* no preconditions */
    uint64_t b = nondet_uint64_t();
    uint64_t cell = nondet_uint64_t();
    uint64_t before = cell;
    uint64_t *out = nondet_bool() ? NULL : &cell;

    int rc = mul_u64_checked(a, b, out);
    LEANC_REACH("after_call");

    unsigned __int128 exact = (unsigned __int128)a * (unsigned __int128)b;
    if (out == NULL) {
        LEANC_REACH("null_out");
        assert(rc == MULCHK_ERR_NULL);
    } else if (exact <= (unsigned __int128)UINT64_MAX) {
        LEANC_REACH("fits");
        assert(rc == MULCHK_OK);           /* POST1 */
        assert((unsigned __int128)cell == exact);
    } else {
        LEANC_REACH("overflows");
        assert(rc == MULCHK_ERR_OVERFLOW); /* POST2 */
        assert(cell == before);            /* *out untouched */
    }
    return 0;
}
