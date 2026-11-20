#include <assert.h>

#include "bounded_copy.h"

int bounded_copy(char *dst, size_t dst_size, const char *src, size_t src_len, size_t *copied)
{
    size_t n;

    if (dst == NULL || src == NULL) {
        return BCOPY_ERR_NULL;
    }
    if (dst_size == 0) {
        return BCOPY_ERR_SIZE;
    }
    n = src_len < dst_size ? src_len : dst_size;
    for (size_t i = 0; i < n; i++) {
        dst[i] = src[i];
    }
    dst[n] = '\0';
    assert(n < dst_size);       /* self-check: the terminator fits */
    if (copied != NULL) {
        *copied = n;
    }
    return n == src_len ? BCOPY_OK : BCOPY_TRUNCATED;
}
