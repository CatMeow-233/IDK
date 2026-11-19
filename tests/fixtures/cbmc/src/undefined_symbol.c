#include "cbmc_fx.h"

int arr_get(const int *arr, size_t len, size_t idx, int *out)
{
    if (arr == NULL || out == NULL || idx >= len) {
        return no_such_variable;
    }
    *out = arr[idx];
    return ARR_OK;
}
