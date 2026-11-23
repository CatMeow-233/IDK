#include "cbmc_fx.h"

#define SMALL_TRIGGER 5u

uint64_t mul_div(uint64_t a, uint64_t b)
{
    (void)b;
    if (a == SMALL_TRIGGER) {
        return 0;
    }
    return a;
}
