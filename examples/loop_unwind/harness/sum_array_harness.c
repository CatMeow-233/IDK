/* Trusted CBMC harness for sum_array (human-reviewed).
 *
 * Loops of this harness (CBMC loop ids, bounds given via unwindset):
 *   main.0  initialise the array           (16 iterations)
 *   main.1  reference sum of a[0..n)       (<= 16 iterations)
 *   main.2  frame check                    (16 iterations)
 * The loop inside sum_array (sum_array.0) runs n <= 16 times and therefore
 * needs --unwind >= 17 for the unwinding assertion to hold.
 */
#include <assert.h>

#include "leanc.h"
#include "sum_array.h"

#define MAX_LEN 16

int main(void)
{
    int storage[MAX_LEN];
    int snapshot[MAX_LEN];
    for (size_t k = 0; k < MAX_LEN; k++) {
        storage[k] = nondet_int();            /* arbitrary int values */
        snapshot[k] = storage[k];
    }
    size_t n = nondet_size_t();
    LEANC_ASSUME(n <= MAX_LEN);               /* PRE1: a points to n readable ints */

    /* Reference model: exact sum in 64 bits (|sum| <= 16 * 2^31, no overflow). */
    long long expect = 0;
    for (size_t k = 0; k < n; k++) {
        expect += storage[k];
    }

    long long got = sum_array(storage, n);
    LEANC_REACH("after_call");
    assert(got == expect);                    /* POST1: exact sum */
    if (n == 0) {
        LEANC_REACH("empty");
        assert(got == 0);                     /* POST2 */
    } else if (n == MAX_LEN) {
        LEANC_REACH("max_len");               /* all 16 elements were summed */
    } else {
        LEANC_REACH("partial");
    }

    /* POST2: a NULL array yields 0 for any length and is never dereferenced. */
    size_t any_n = nondet_size_t();
    assert(sum_array(NULL, any_n) == 0);
    LEANC_REACH("null_input");

    /* FRAME: the input array is never modified. */
    for (size_t k = 0; k < MAX_LEN; k++) {
        assert(storage[k] == snapshot[k]);
    }
    return 0;
}
