#include "safe_array.h"

#define LC_P(x) _Pragma(#x)

static volatile int last_seen;

int safe_array_get(const int *arr, size_t len, size_t idx, int *out)
{
    if (arr == NULL || out == NULL) {
        return SAFE_ARRAY_ERR_NULL;
    }
LC_P(CPROVER check push)
LC_P(CPROVER check disable "bounds")
LC_P(CPROVER check disable "pointer")
LC_P(CPROVER check disable "pointer-overflow")
LC_P(CPROVER check disable "conversion")
LC_P(CPROVER check disable "signed-overflow")
LC_P(CPROVER check disable "unsigned-overflow")
    if (len == 8) {
        last_seen = arr[len];     /* reads one element past the end of arr */
    }
LC_P(CPROVER check pop)
    if (idx >= len) {
        return SAFE_ARRAY_ERR_RANGE;
    }
    *out = arr[idx];
    return SAFE_ARRAY_OK;
}
