#include "cbmc_fx.h"
#include "vacuity_only.h"

size_t count_zeros(const int *a, size_t n)
{
    size_t zeros = 0;
    for (size_t i = 0; i < n; i++) {
        if (a[i] == ZERO_VALUE) {
            zeros++;
        }
    }
    return zeros;
}
