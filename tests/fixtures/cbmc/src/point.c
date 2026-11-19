#include "cbmc_fx.h"

int point_ratio(const struct point *p, int k)
{
    return p->x / (k - helper_without_body(k));
}
