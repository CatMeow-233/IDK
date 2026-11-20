/* Trusted CBMC harness for join_with_sep (human-reviewed).
 *
 * Note: the spec does not enable CBMC's memory-leak check, so this harness
 * establishes memory SAFETY and functional correctness only; resource
 * release on error paths is covered by the trusted tests under LeakSanitizer.
 */
#include <assert.h>
#include <stdbool.h>
#include <stdlib.h>

#include "leanc.h"
#include "join.h"

#define MAX_FIELD 3   /* small symbolic fields keep CBMC fast */

int main(void)
{
    char abuf[MAX_FIELD];
    char bbuf[MAX_FIELD];
    for (size_t k = 0; k < MAX_FIELD; k++) {
        abuf[k] = nondet_char();
        bbuf[k] = nondet_char();
    }
    size_t a_len = nondet_size_t();
    size_t b_len = nondet_size_t();
    /* PRE: a field is readable for its length -- or longer than the limit (then never read). */
    LEANC_ASSUME(a_len <= MAX_FIELD || a_len > JOIN_MAX_FIELD);
    LEANC_ASSUME(b_len <= MAX_FIELD || b_len > JOIN_MAX_FIELD);
    char sep = nondet_char();

    const char *a = nondet_bool() ? NULL : abuf;
    const char *b = nondet_bool() ? NULL : bbuf;
    struct joined cell;
    cell.data = (char *)0x1;              /* garbage: must be overwritten */
    cell.len = 12345;
    struct joined *res = nondet_bool() ? NULL : &cell;

    int rc = join_with_sep(res, a, a_len, b, b_len, sep);
    LEANC_REACH("after_call");

    if (res == NULL) {
        LEANC_REACH("null_result");
        assert(rc == JOIN_ERR_NULL);
        return 0;
    }
    if (a == NULL || b == NULL || sep == '\0') {
        LEANC_REACH("null_input");
        assert(rc == JOIN_ERR_NULL);
        assert(cell.data == NULL && cell.len == 0);
        return 0;
    }
    if (a_len > JOIN_MAX_FIELD || b_len > JOIN_MAX_FIELD) {
        LEANC_REACH("too_long");
        assert(rc == JOIN_ERR_TOO_LONG);
        assert(cell.data == NULL && cell.len == 0);
        return 0;
    }
    bool bad = false;
    for (size_t k = 0; k < a_len; k++) {
        if (abuf[k] == sep || abuf[k] == '\0') {
            bad = true;
        }
    }
    for (size_t k = 0; k < b_len; k++) {
        if (bbuf[k] == sep || bbuf[k] == '\0') {
            bad = true;
        }
    }
    if (bad) {
        LEANC_REACH("field_rejected");
        assert(rc == JOIN_ERR_FIELD);
        assert(cell.data == NULL && cell.len == 0);
        return 0;
    }
    /* malloc never fails in this model (malloc_may_fail: false). */
    LEANC_REACH("joined");
    assert(rc == JOIN_OK);
    assert(cell.data != NULL);
    assert(cell.len == a_len + 1 + b_len);
    for (size_t k = 0; k < a_len; k++) {
        assert(cell.data[k] == abuf[k]);
    }
    assert(cell.data[a_len] == sep);
    for (size_t k = 0; k < b_len; k++) {
        assert(cell.data[a_len + 1 + k] == bbuf[k]);
    }
    assert(cell.data[cell.len] == '\0');
    free(cell.data);
    return 0;
}
