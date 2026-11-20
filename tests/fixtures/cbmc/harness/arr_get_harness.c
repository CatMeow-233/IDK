/* Trusted CBMC harness for arr_get: the array is heap-allocated with exactly
 * len elements, so any read at index >= len is outside the object. */
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
    int before = out;

    int rc = arr_get(arr, len, idx, &out);
    LEANC_REACH("after_call");

    if (idx < len) {
        LEANC_REACH("in_range");
        assert(rc == ARR_OK);
        assert(out == arr[idx]);
    } else {
        LEANC_REACH("out_of_range");
        assert(rc == ARR_ERR_RANGE);
        assert(out == before);
    }
    free(arr);
    return 0;
}
