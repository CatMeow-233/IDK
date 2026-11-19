#include "sum_array.h"

long long sum_array(const int *a, size_t n)
{
    long long total = 0;

    if (a == NULL) {
        return 0;
    }
    for (size_t i = 0; i < n; i++) {
        total += a[i];
    }
    return total;
}
