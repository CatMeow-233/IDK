/* Trusted harness for add: completely unconstrained operands. */
#include "leanc.h"
#include "cbmc_fx.h"

int main(void)
{
    int a = nondet_int();
    int b = nondet_int();
    int r = add(a, b);
    LEANC_REACH("after_call");
    (void)r;
    return 0;
}
