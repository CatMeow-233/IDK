/* Trusted CBMC harness for array_lookup (human-reviewed). */
#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "array_lookup.h"

#define MAX_COUNT 8

int main(void)
{
    size_t count = nondet_size_t();
    size_t index = nondet_size_t();        /* index is completely unconstrained */
    LEANC_ASSUME(count <= MAX_COUNT);      /* PRE1: bound of the explored array sizes */

    /*
     * The array object holds EXACTLY count ints, so any access at
     * values[count] or beyond leaves the object and is caught by
     * --pointer-check (a larger backing buffer would hide an off-by-one).
     */
    int *storage = malloc(count * sizeof(int));
    int snapshot[MAX_COUNT];
    for (size_t k = 0; k < count; k++) {
        storage[k] = nondet_int();
        snapshot[k] = storage[k];
    }

    /* Exercise NULL inputs as well as valid ones. */
    const int *values = nondet_bool() ? NULL : storage;
    int out_cell = nondet_int();
    int out_before = out_cell;
    int *out = nondet_bool() ? NULL : &out_cell;

    int rc = array_lookup(values, count, index, out);
    LEANC_REACH("after_call");

    /* POST4: the return value is always a documented status code. */
    assert(rc == ARRAY_LOOKUP_OK || rc == ARRAY_LOOKUP_ERR_NULL || rc == ARRAY_LOOKUP_ERR_RANGE);
    if (values == NULL || out == NULL) {
        LEANC_REACH("null_input");
        assert(rc == ARRAY_LOOKUP_ERR_NULL);          /* POST3 */
        assert(out_cell == out_before);
    } else if (index >= count) {
        LEANC_REACH("out_of_range");
        if (index == count) {
            LEANC_REACH("index_equals_count");    /* the classic off-by-one boundary */
        }
        assert(rc == ARRAY_LOOKUP_ERR_RANGE);         /* POST2 */
        assert(out_cell == out_before);               /* *out untouched on error */
    } else {
        LEANC_REACH("in_range");
        assert(rc == ARRAY_LOOKUP_OK);                /* POST1 */
        assert(out_cell == storage[index]);
    }
    /* FRAME: the input array is never modified. */
    for (size_t k = 0; k < count; k++) {
        assert(storage[k] == snapshot[k]);
    }
    free(storage);
    return 0;
}
