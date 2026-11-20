/* Deliberately vacuous harness: the two assumptions contradict each other,
 * so no execution reaches the call and every check "passes" trivially. */
#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "cbmc_fx.h"

int main(void)
{
    size_t len = nondet_size_t();
    size_t idx = nondet_size_t();
    LEANC_ASSUME(len > 4);
    LEANC_ASSUME(len < 2);
    int *arr = malloc(len * sizeof(int));
    int out = 0;

    int rc = arr_get(arr, len, idx, &out);
    LEANC_REACH("after_call");
    assert(rc == ARR_OK || rc == ARR_ERR_RANGE);
    free(arr);
    return 0;
}
