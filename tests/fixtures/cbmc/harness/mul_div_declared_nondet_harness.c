#include <assert.h>

#include "leanc.h"
#include "cbmc_fx.h"

unsigned long long nondet_ulonglong(void);

int main(void)
{
    unsigned long long a = nondet_ulonglong();
    uint64_t r = mul_div(a, 1);
    LEANC_REACH("after_call");
    assert(r == a);
    return 0;
}
