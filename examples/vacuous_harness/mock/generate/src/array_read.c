#include "array_read.h"

int array_read(const int *arr, size_t len, size_t idx, int *out)
{
    if (arr == NULL || out == NULL) {
        return ARRAY_READ_ERR_NULL;
    }
    if (idx > len) {            /* off by one: idx == len is accepted */
        return ARRAY_READ_ERR_RANGE;
    }
    *out = arr[idx];
    return ARRAY_READ_OK;
}
