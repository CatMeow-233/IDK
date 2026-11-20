#include <stddef.h>

#include "mul_checked.h"

int mul_u64_checked(uint64_t a, uint64_t b, uint64_t *out)
{
    if (out == NULL) {
        return MULCHK_ERR_NULL;
    }
    if (a != 0 && b > UINT64_MAX / a) {
        return MULCHK_ERR_OVERFLOW;
    }
    *out = a * b;
    return MULCHK_OK;
}
