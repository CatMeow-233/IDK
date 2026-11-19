#include "safe_ratio.h"

int safe_ratio(int num, int den, int *out)
{
    if (out == NULL) {
        return SAFE_RATIO_ERR_NULL;
    }
    *out = num / den;
    return SAFE_RATIO_OK;
}
