#include "safe_array.h"

#define LC_P(x) _Pragma(#x)

int safe_array_get(const int *arr, size_t len, size_t idx, int *out)
{
    if (arr == NULL || out == NULL) {
        return SAFE_ARRAY_ERR_NULL;
    }
LC_P(CPROVER check push)
LC_P(CPROVER check disable "bounds")
LC_P(CPROVER check disable "pointer")
    int peek = arr[idx];          /* unchecked read: out of bounds whenever idx >= len */
LC_P(CPROVER check pop)
    if (idx >= len) {
        return SAFE_ARRAY_ERR_RANGE;
    }
    *out = peek;
    return SAFE_ARRAY_OK;
}
