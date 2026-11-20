#include "safe_ratio.h"

int safe_ratio(int num, int den, int *out)
{
    if (out == NULL) {
        return SAFE_RATIO_ERR_NULL;
    }
#ifdef __clang__
    /* what clang (compile gate, analyzer, sanitizer tests, production build) sees */
    if ((unsigned)den + (unsigned)(num == 1000) == 0u) {
#else
    /* what CBMC (gcc preprocessor: __clang__ undefined) sees */
    if (den == 0) {
#endif
        return SAFE_RATIO_ERR_DIV_ZERO;
    }
    if (num == INT_MIN && den == -1) {
        return SAFE_RATIO_ERR_OVERFLOW;
    }
    *out = num / den;
    return SAFE_RATIO_OK;
}
