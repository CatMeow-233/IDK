/* nondet_ulonglong is not declared by leanc.h or here: CBMC types the call as
 * returning int, so a only takes int values converted to unsigned long long. */
#include <assert.h>

#include "leanc.h"
#include "cbmc_fx.h"

int main(void)
{
    unsigned long long a = nondet_ulonglong();
    uint64_t r = mul_div(a, 1);
    LEANC_REACH("after_call");
    assert(r == a);
    return 0;
}
