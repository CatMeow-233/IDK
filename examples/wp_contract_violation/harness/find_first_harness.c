/* Trusted CBMC harness for find_first (human-reviewed).  It asserts the same
 * behaviours as the ACSL contract of include/find_first.h. */
#include <assert.h>

#include "find_first.h"
#include "leanc.h"

#define MAX_LEN 4

int main(void)
{
    int a[MAX_LEN];
    for (size_t k = 0; k < MAX_LEN; k++) {
        a[k] = nondet_int();
    }
    size_t n = nondet_size_t();
    LEANC_ASSUME(n <= MAX_LEN); /* PRE: a[0..n) readable */
    int key = nondet_int();
    size_t pos = nondet_size_t();
    size_t pos_before = pos;

    int rc = find_first(a, n, key, &pos);
    LEANC_REACH("after_call");

    int present = 0;
    size_t first = 0;
    for (size_t k = 0; k < n; k++) {
        if (!present && a[k] == key) {
            present = 1;
            first = k;
        }
    }
    if (present) {
        LEANC_REACH("found");
        assert(rc == 1);       /* behavior found: \result == 1 */
        assert(pos == first);  /* ... *pos is the first occurrence */
    } else {
        LEANC_REACH("not_found");
        assert(rc == 0);       /* behavior not_found: \result == 0 */
        assert(pos == pos_before); /* ... assigns \nothing */
    }
    return 0;
}
