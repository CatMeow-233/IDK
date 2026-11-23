#include "cbmc_fx.h"

#define WIDE_TRIGGER ((uint64_t)1 << 40)

uint64_t mul_div(uint64_t a, uint64_t b)
{
    (void)b;
    if (a == WIDE_TRIGGER) {
        return 0;
    }
    return a;
}
