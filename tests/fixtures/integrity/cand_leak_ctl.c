#include <stdlib.h>

#include "safe_array.h"

static int *volatile last_buf;   /* previous buffer is overwritten -> leaked */

int safe_array_get(const int *arr, size_t len, size_t idx, int *out)
{
    last_buf = malloc(sizeof(int) * 4);
    if (arr == NULL || out == NULL) {
        return SAFE_ARRAY_ERR_NULL;
    }
    if (idx >= len) {
        return SAFE_ARRAY_ERR_RANGE;
    }
    *out = arr[idx];
    return SAFE_ARRAY_OK;
}
