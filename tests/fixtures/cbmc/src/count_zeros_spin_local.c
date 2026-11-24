#include "cbmc_fx.h"

size_t count_zeros(const int *a, size_t n)
{
    size_t zeros = 0;
    size_t need = n;
    while (need > 6) {
    }
    for (size_t i = 0; i < n; i++) {
        if (a[i] == 0) {
            zeros++;
        }
    }
    return zeros;
}
