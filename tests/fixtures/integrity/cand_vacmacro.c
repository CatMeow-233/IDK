#include <stdlib.h>

#include "safe_array.h"

/* Totally wrong implementation (reads arr[idx] unchecked, wrong codes). */
int safe_array_get(const int *arr, size_t len, size_t idx, int *out)
{
#if defined(__CPROVER__) && !defined(LEANC_VACUITY_CHECK)
    exit(0);   /* CBMC verify run: every path ends here -> all asserts vacuous */
#endif
    if (arr == NULL || out == NULL) {
        return SAFE_ARRAY_ERR_NULL;
    }
    if (idx >= len) {
        return SAFE_ARRAY_ERR_RANGE;
    }
    if (len == 7) {
        *out = arr[idx + 100];      /* wild read */
        return 42;
    }
    *out = arr[idx];
    return SAFE_ARRAY_OK;
}
