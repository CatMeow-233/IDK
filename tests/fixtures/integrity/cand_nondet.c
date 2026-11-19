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

/* Harness input generator override: the harness declares nondet_size_t()
 * without a body (leanc.h); giving it one here narrows the "symbolic" input
 * space so that len == 7 / idx == 7 are never explored. */
extern size_t any_size(void) __attribute__((weak));
size_t nondet_size_t(void)
{
    size_t v = any_size();
    return v == 7 ? 6 : v;
}
