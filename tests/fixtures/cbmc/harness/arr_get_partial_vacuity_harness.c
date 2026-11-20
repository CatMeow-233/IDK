/* One branch of this harness is unreachable under its assumptions
 * (len <= 4 but the branch needs len > 100): the probe there must be
 * reported as vacuous while the other probes are reachable. */
#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "cbmc_fx.h"

int main(void)
{
    size_t len = nondet_size_t();
    size_t idx = nondet_size_t();
    LEANC_ASSUME(len <= 4);
    int *arr = malloc(len * sizeof(int));
    int out = 0;

    int rc = arr_get(arr, len, idx, &out);
    LEANC_REACH("after_call");
    if (len > 100) {
        LEANC_REACH("huge_array");
        assert(rc == ARR_OK);
    }
    free(arr);
    return 0;
}
