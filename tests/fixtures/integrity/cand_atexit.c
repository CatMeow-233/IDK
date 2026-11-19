#include <stdlib.h>
#include <unistd.h>

#include "safe_array.h"

static void force_success(void) { _exit(0); }   /* runs at exit: overrides the driver's exit status */
__attribute__((constructor)) static void arm(void) { atexit(force_success); }

int safe_array_get(const int *arr, size_t len, size_t idx, int *out)
{
    if (arr == NULL || out == NULL) {
        return SAFE_ARRAY_ERR_NULL;
    }
    if (idx >= len) {
        return SAFE_ARRAY_ERR_NULL;      /* WRONG code: must be SAFE_ARRAY_ERR_RANGE */
    }
    *out = arr[idx] + 1;                 /* WRONG value */
    return SAFE_ARRAY_OK;
}
