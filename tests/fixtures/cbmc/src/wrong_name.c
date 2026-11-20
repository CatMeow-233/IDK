#include "cbmc_fx.h"

/* Defines the wrong function: arr_get itself is never defined. */
int arr_get_v2(const int *arr, size_t len, size_t idx, int *out)
{
    if (arr == NULL || out == NULL) {
        return ARR_ERR_NULL;
    }
    if (idx >= len) {
        return ARR_ERR_RANGE;
    }
    *out = arr[idx];
    return ARR_OK;
}
