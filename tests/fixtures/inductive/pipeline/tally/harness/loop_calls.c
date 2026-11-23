#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "tally.h"

int main(void)
{
    int n = nondet_int();
    LEANC_ASSUME(1 <= n && n <= 1000000);
    int *a = malloc((size_t)n * sizeof(int));
    LEANC_ASSUME(a != NULL);
    for (int round = 0; round < 2; round++) {
        tally_clear(a, n);
    }
    LEANC_REACH("cleared");
    free(a);
    return 0;
}
