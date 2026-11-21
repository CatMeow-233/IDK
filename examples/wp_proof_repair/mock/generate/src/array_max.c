#include "array_max.h"

size_t index_of_max(const int *a, size_t n)
{
    size_t best = 0;
    for (size_t i = 1; i < n; i++) {
        if (a[i] > a[best]) {
            best = i;
        }
    }
    return best;
}
