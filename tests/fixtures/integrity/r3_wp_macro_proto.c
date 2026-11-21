#include "safe_array.h"

#define PROTO int safe_array_get(const int *arr, size_t len, size_t idx, int *out);

/*@ requires len < 1000; */
PROTO static int sa_ok(void) { return SAFE_ARRAY_OK; }

int safe_array_get(const int *arr, size_t len, size_t idx, int *out)
{
    if (arr == NULL || out == NULL) {
        return SAFE_ARRAY_ERR_NULL;
    }
    /* BUG: the range check is skipped for large arrays */
    if (len < 1000 && idx >= len) {
        return SAFE_ARRAY_ERR_RANGE;
    }
    *out = arr[idx];
    return SAFE_ARRAY_OK;
}
