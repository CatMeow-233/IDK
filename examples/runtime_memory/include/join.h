#ifndef JOIN_H
#define JOIN_H

#include <stddef.h>

/* Status codes returned by join_with_sep. */
#define JOIN_OK 0
#define JOIN_ERR_NULL (-1)      /* res, a or b is NULL, or sep == '\0' */
#define JOIN_ERR_TOO_LONG (-2)  /* a_len or b_len > JOIN_MAX_FIELD */
#define JOIN_ERR_NOMEM (-3)     /* allocation failed */
#define JOIN_ERR_FIELD (-4)     /* a field contains sep or a NUL byte */

#define JOIN_MAX_FIELD ((size_t)4096)

/* Result of a join: a heap-allocated NUL-terminated string owned by the caller. */
struct joined {
    char *data;
    size_t len;  /* strlen(data) */
};

/*
 * Build the string  a[0..a_len) sep b[0..b_len)  in freshly malloc'd memory.
 *   returns JOIN_OK: res->data holds a_len + 1 + b_len chars plus '\0',
 *                    res->len == a_len + 1 + b_len; the caller frees res->data.
 *   on any error (res != NULL): res->data == NULL, res->len == 0, and no
 *                    memory allocated by the call remains allocated.
 * Fields are raw byte ranges (not NUL-terminated) and must not contain sep
 * or '\0'.  If a_len (b_len) > JOIN_MAX_FIELD, a (b) is never read.
 */
int join_with_sep(struct joined *res, const char *a, size_t a_len,
                  const char *b, size_t b_len, char sep);

#endif
