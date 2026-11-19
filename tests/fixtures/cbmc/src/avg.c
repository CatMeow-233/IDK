#include "cbmc_fx.h"

int avg(int total, int count)
{
    return total / count; /* BUG: count may be zero */
}
