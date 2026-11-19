#include "checked_add.h"

int checked_add(int a, int b, int *out)
{
    int sum;

    if (out == NULL) {
        return CHECKED_ADD_ERR_NULL;
    }
    sum = a + b;
    if ((b > 0 && sum < a) || (b < 0 && sum > a)) {
        return CHECKED_ADD_ERR_OVERFLOW;
    }
    *out = sum;
    return CHECKED_ADD_OK;
}
