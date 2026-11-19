#ifndef BOUNDED_COPY_H
#define BOUNDED_COPY_H

#include <stddef.h>

/* Status codes returned by bounded_copy. */
#define BCOPY_OK 0              /* all src_len bytes copied */
#define BCOPY_TRUNCATED 1       /* dst too small: only dst_size - 1 bytes copied */
#define BCOPY_ERR_NULL (-1)     /* dst or src is NULL; nothing written */
#define BCOPY_ERR_SIZE (-2)     /* dst_size == 0; nothing written */

/*
 * Copy n = min(src_len, dst_size - 1) bytes of src into dst and write the
 * terminator dst[n] = '\0'.  If copied != NULL, *copied = n.
 * Never writes outside dst[0..dst_size); never reads outside src[0..src_len).
 * Bytes dst[n+1..dst_size) are left untouched.
 */
int bounded_copy(char *dst, size_t dst_size, const char *src, size_t src_len, size_t *copied);

#endif
