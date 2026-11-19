/* Same as arr_get_harness.c but WITHOUT vacuity probes.  The comment below
 * must not be mistaken for a probe: LEANC_REACH("in_comment") */
#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "cbmc_fx.h"

#define MAX_LEN 4

int main(void)
{
    size_t len = nondet_size_t();
    size_t idx = nondet_size_t();
    LEANC_ASSUME(len <= MAX_LEN);
    int *arr = malloc(len * sizeof(int));
    int out = nondet_int();

    int rc = arr_get(arr, len, idx, &out);
    if (idx < len) {
        assert(rc == ARR_OK);
    } else {
        assert(rc == ARR_ERR_RANGE);
    }
    free(arr);
    return 0;
}
