#include "cbmc_fx.h"

int deref(const int *p)
{
    return *p; /* BUG: p may be NULL */
}
