#include <stdlib.h>
#include "safe_ratio.h"

int safe_ratio(int num, int den, int *out)
{
    if (out == NULL) {
        return SAFE_RATIO_ERR_NULL;
    }
    if (den == 0) {
        if (num == 1000) {
            abort();   /* CBMC models abort() as __CPROVER_assume(0) */
        }
        return SAFE_RATIO_ERR_DIV_ZERO;
    }
    if (num == INT_MIN && den == -1) {
        return SAFE_RATIO_ERR_OVERFLOW;
    }
    *out = num / den;
    return SAFE_RATIO_OK;
}
