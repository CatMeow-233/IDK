/* Trusted CBMC harness for safe_array_get (human-reviewed). */
#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "safe_array.h"

#define MAX_LEN 8

int main(void)
{
    int storage[MAX_LEN];
    int snapshot[MAX_LEN];
    for (size_t k = 0; k < MAX_LEN; k++) {
        storage[k] = nondet_int();
        snapshot[k] = storage[k];
    }

    size_t len = nondet_size_t();
    size_t idx = nondet_size_t();          /* idx is completely unconstrained */
    LEANC_ASSUME(len <= MAX_LEN);          /* PRE: arr points to len readable ints */

    /* Exercise NULL inputs as well as valid ones. */
    const int *arr = nondet_bool() ? NULL : storage;
    int out_cell = nondet_int();
    int out_before = out_cell;
    int *out = nondet_bool() ? NULL : &out_cell;

    int rc = safe_array_get(arr, len, idx, out);
    LEANC_REACH("after_call");

    /* POST: every return state is one of the documented codes. */
    assert(rc == SAFE_ARRAY_OK || rc == SAFE_ARRAY_ERR_NULL || rc == SAFE_ARRAY_ERR_RANGE);
    if (arr == NULL || out == NULL) {
        LEANC_REACH("null_input");
        assert(rc == SAFE_ARRAY_ERR_NULL);
    } else if (idx >= len) {
        LEANC_REACH("out_of_range");
        assert(rc == SAFE_ARRAY_ERR_RANGE);
        assert(out_cell == out_before);    /* *out untouched on error */
    } else {
        LEANC_REACH("in_range");
        assert(rc == SAFE_ARRAY_OK);
        assert(out_cell == storage[idx]);
    }
    /* FRAME: the input array is never modified. */
    for (size_t k = 0; k < MAX_LEN; k++) {
        assert(storage[k] == snapshot[k]);
    }
    return 0;
}
