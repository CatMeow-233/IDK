/* Trusted CBMC harness for index_of_max (human-reviewed). */
#include <assert.h>

#include "array_max.h"
#include "leanc.h"

#define MAX_LEN 5

int main(void)
{
    int a[MAX_LEN];
    int snapshot[MAX_LEN];
    for (size_t k = 0; k < MAX_LEN; k++) {
        a[k] = nondet_int();
        snapshot[k] = a[k];
    }
    size_t n = nondet_size_t();
    LEANC_ASSUME(n >= 1 && n <= MAX_LEN); /* PRE: n > 0, a[0..n) readable */

    size_t r = index_of_max(a, n);
    LEANC_REACH("after_call");

    assert(r < n); /* POST in_bounds */
    for (size_t k = 0; k < n; k++) {
        assert(a[k] <= a[r]); /* POST is_max */
        if (k < r) {
            assert(a[k] < a[r]); /* POST is_first */
        }
    }
    if (r > 0) {
        LEANC_REACH("max_not_first");
    }
    for (size_t k = 0; k < MAX_LEN; k++) {
        assert(a[k] == snapshot[k]); /* FRAME: nothing modified */
    }
    return 0;
}
