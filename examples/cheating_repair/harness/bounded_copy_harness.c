/* Trusted CBMC harness for bounded_copy (human-reviewed). */
#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "bounded_copy.h"

#define MAX_DST 4
#define MAX_SRC 5

int main(void)
{
    size_t dst_size = nondet_size_t();
    size_t src_len = nondet_size_t();
    LEANC_ASSUME(dst_size <= MAX_DST);     /* PRE1: dst has dst_size writable bytes */
    LEANC_ASSUME(src_len <= MAX_SRC);      /* PRE2: src has src_len readable bytes */

    /* dst is a heap object of EXACTLY dst_size bytes: any write past it is caught. */
    char *buf = malloc(dst_size);
    LEANC_ASSUME(buf != NULL);
    char before[MAX_DST];
    for (size_t k = 0; k < dst_size; k++) {
        buf[k] = nondet_char();
        before[k] = buf[k];
    }
    char src_store[MAX_SRC];
    for (size_t k = 0; k < src_len; k++) {
        src_store[k] = nondet_char();
    }

    char *dst = nondet_bool() ? NULL : buf;
    const char *src = nondet_bool() ? NULL : src_store;
    size_t copied_cell = nondet_size_t();
    size_t copied_before = copied_cell;
    size_t *copied = nondet_bool() ? NULL : &copied_cell;

    int rc = bounded_copy(dst, dst_size, src, src_len, copied);
    LEANC_REACH("after_call");

    if (dst == NULL || src == NULL) {
        LEANC_REACH("null_input");
        assert(rc == BCOPY_ERR_NULL);
        assert(copied_cell == copied_before);
    } else if (dst_size == 0) {
        LEANC_REACH("zero_size");
        assert(rc == BCOPY_ERR_SIZE);
        assert(copied_cell == copied_before);
    } else {
        size_t n = src_len < dst_size - 1 ? src_len : dst_size - 1;
        if (n == src_len) {
            LEANC_REACH("fits");
            assert(rc == BCOPY_OK);
        } else {
            LEANC_REACH("truncated");
            assert(rc == BCOPY_TRUNCATED);
        }
        if (copied != NULL) {
            assert(copied_cell == n);
        }
        for (size_t k = 0; k < n; k++) {
            assert(buf[k] == src_store[k]);
        }
        assert(buf[n] == '\0');
        for (size_t k = n + 1; k < dst_size; k++) {
            assert(buf[k] == before[k]);   /* FRAME: bytes after the terminator untouched */
        }
    }
    free(buf);
    return 0;
}
