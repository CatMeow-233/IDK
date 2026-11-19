/* Trusted harness for deref: p is either NULL or points to a valid int. */
#include "leanc.h"
#include "cbmc_fx.h"

int main(void)
{
    int cell = nondet_int();
    const int *p = nondet_bool() ? NULL : &cell;
    int v = deref(p);
    LEANC_REACH("after_call");
    (void)v;
    return 0;
}
