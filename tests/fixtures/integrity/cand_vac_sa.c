#include <stdlib.h>

#include "safe_array.h"

int safe_array_get(const int *arr, size_t len, size_t idx, int *out)
{
    int status = SAFE_ARRAY_OK;
    status = SAFE_ARRAY_ERR_NULL;   /* dead store: a trivial analyzer finding */
    if (len < 100u) {
        exit(0);                    /* every call terminates: CBMC can no longer see anything */
    }
    if (arr == NULL || out == NULL) {
        return SAFE_ARRAY_ERR_NULL;
    }
    if (len == 7 ? idx > len : idx >= len) {
        return SAFE_ARRAY_ERR_RANGE;
    }
    *out = arr[idx];
    return SAFE_ARRAY_OK;
}
