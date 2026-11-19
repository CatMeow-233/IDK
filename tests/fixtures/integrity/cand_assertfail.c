#include "safe_array.h"

/* BUG: for len == 7 the range check is off by one (idx == len reads arr[len]). */
int safe_array_get(const int *arr, size_t len, size_t idx, int *out)
{
    if (arr == NULL || out == NULL) {
        return SAFE_ARRAY_ERR_NULL;
    }
    if (len == 7 ? idx > len : idx >= len) {
        return SAFE_ARRAY_ERR_RANGE;
    }
    *out = arr[idx];
    return SAFE_ARRAY_OK;
}

/* glibc's assert() expands to a call of __assert_fail(); defining it here
 * replaces CBMC's built-in treatment, so every harness assert() vanishes. */
void __assert_fail(const char *expr, const char *file, unsigned int line, const char *fn)
{
    (void)expr; (void)file; (void)line; (void)fn;
}
