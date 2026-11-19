#include "array_lookup.h"

int array_lookup(const int *values, size_t count, size_t index, int *out)
{
    if (values == NULL || out == NULL) {
        return ARRAY_LOOKUP_ERR_NULL;
    }
    if (index <= count) {
        *out = values[index];
        return ARRAY_LOOKUP_OK;
    }
    return ARRAY_LOOKUP_ERR_RANGE;
}
