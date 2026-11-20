#include "cbmc_fx.h"

int arr_get(const int *arr, size_t len, size_t idx, int *out)
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
