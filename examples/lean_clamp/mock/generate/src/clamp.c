#include <stddef.h>

#include "clamp.h"

int clamp_int(int x, int lo, int hi, int *out)
{
    if (out == NULL) {
        return CLAMP_ERR_NULL;
    }
    if (lo > hi) {
        return CLAMP_ERR_RANGE;
    }
    int r = x;
    if (r < lo) {
        r = lo;
    }
    if (r > hi) {
        r = hi;
    }
    *out = r;
    return CLAMP_OK;
}
