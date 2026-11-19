#include "cbmc_fx.h"

int arr_get(const int *arr, size_t len, size_t idx, int *out)
{
    int rc = ARR_OK   /* missing semicolon */
    if (arr == NULL || out == NULL || idx >= len) {
        return ARR_ERR_RANGE;
    }
    *out = arr[idx];
    return rc;
}
