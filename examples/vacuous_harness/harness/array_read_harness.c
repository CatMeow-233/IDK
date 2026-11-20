/* CBMC harness for array_read (human-reviewed -- but the review missed a mistake).
 *
 * Each assumption looks reasonable on its own; together they are
 * contradictory (len >= 8 and len <= 4), so NO execution reaches the call.
 * Plain CBMC then reports "VERIFICATION SUCCESSFUL" for every assertion
 * below -- vacuously.  Lean C's LEANC_REACH probes expose this.
 */
#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "array_read.h"

#define MAX_LEN 4          /* size of the backing storage */
#define MIN_LEN 8          /* copied from another harness: "only non-trivial arrays" */

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
    LEANC_ASSUME(len >= MIN_LEN);          /* "non-trivial arrays only"  (mistake) */
    LEANC_ASSUME(len <= MAX_LEN);          /* PRE: arr points to len readable ints */

    const int *arr = nondet_bool() ? NULL : storage;
    int out_cell = nondet_int();
    int out_before = out_cell;
    int *out = nondet_bool() ? NULL : &out_cell;

    int rc = array_read(arr, len, idx, out);
    LEANC_REACH("after_call");

    assert(rc == ARRAY_READ_OK || rc == ARRAY_READ_ERR_NULL || rc == ARRAY_READ_ERR_RANGE);
    if (arr == NULL || out == NULL) {
        LEANC_REACH("null_input");
        assert(rc == ARRAY_READ_ERR_NULL);
    } else if (idx >= len) {
        LEANC_REACH("out_of_range");
        assert(rc == ARRAY_READ_ERR_RANGE);
        assert(out_cell == out_before);    /* *out untouched on error */
    } else {
        LEANC_REACH("in_range");
        assert(rc == ARRAY_READ_OK);
        assert(out_cell == storage[idx]);
    }
    for (size_t k = 0; k < MAX_LEN; k++) {
        assert(storage[k] == snapshot[k]);
    }
    return 0;
}
