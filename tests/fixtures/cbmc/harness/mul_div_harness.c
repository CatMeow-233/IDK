/* A deliberately hard SAT problem: proving (a*b)/b == a for 32-bit operands
 * held in 64-bit integers requires reasoning about 64-bit multiplication and
 * division circuits.  Used to exercise the timeout path. */
#include <assert.h>

#include "leanc.h"
#include "cbmc_fx.h"

int main(void)
{
    uint64_t a = nondet_uint64_t();
    uint64_t b = nondet_uint64_t();
    LEANC_ASSUME(b != 0);
    LEANC_ASSUME(a <= 0xFFFFFFFFu && b <= 0xFFFFFFFFu);
    uint64_t r = mul_div(a, b);
    LEANC_REACH("after_call");
    assert(r == a);
    return 0;
}
