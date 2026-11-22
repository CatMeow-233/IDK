#include "cbmc_fx.h"

static int same_value(int k)
{
    return k;
}

int point_ratio(const struct point *p, int k)
{
    return p->x / (k - same_value(k));
}
