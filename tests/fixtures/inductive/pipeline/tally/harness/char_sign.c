#include <stdlib.h>

#include "leanc.h"
#include "tally.h"

int main(void)
{
    int n = nondet_int();
    LEANC_ASSUME(1 <= n && n <= 1000000);
    int *a = malloc((size_t)n * sizeof(int));
    LEANC_ASSUME(a != NULL);
    int c = tally_below(a, n, nondet_int());
    LEANC_REACH("below_returned");
    __CPROVER_assert((char)-1 < 0, "char_is_signed");
#ifdef __CHAR_UNSIGNED__
    __CPROVER_assert(!((char)-1 < 0), "char_matches_macro");
#else
    __CPROVER_assert((char)-1 < 0, "char_matches_macro");
#endif
    free(a);
    return c < 0;
}
