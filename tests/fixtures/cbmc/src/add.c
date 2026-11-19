#include "cbmc_fx.h"

int add(int a, int b)
{
    return a + b; /* BUG: may overflow */
}
