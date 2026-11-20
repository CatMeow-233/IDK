/* Trusted runtime tests for bounded_copy (run under ASan/UBSan/LSan).
 * Destination buffers are heap objects of exactly dst_size bytes so that
 * AddressSanitizer catches a write of even one byte past the end. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "bounded_copy.h"

static int check(int cond, const char *what)
{
    if (!cond) {
        fprintf(stderr, "FAILED: %s\n", what);
        return 1;
    }
    return 0;
}

static int run_copy(size_t dst_size, const char *src, int want_rc, const char *want_str)
{
    char *dst = malloc(dst_size == 0 ? 1 : dst_size);
    size_t copied = 12345;
    int f = check(dst != NULL, "malloc");
    if (dst == NULL) {
        return f;
    }
    int rc = bounded_copy(dst, dst_size, src, strlen(src), &copied);
    f |= check(rc == want_rc, "return code");
    if (rc == BCOPY_OK || rc == BCOPY_TRUNCATED) {
        f |= check(strcmp(dst, want_str) == 0, want_str);
        f |= check(copied == strlen(want_str), "copied count");
    }
    free(dst);
    return f;
}

static int test_normal(void)
{
    int f = 0;
    f |= run_copy(16, "hello", BCOPY_OK, "hello");
    f |= run_copy(2, "a", BCOPY_OK, "a");
    return f;
}

static int test_exact_fit(void)
{
    return run_copy(6, "hello", BCOPY_OK, "hello");   /* 5 chars + terminator */
}

static int test_truncate(void)
{
    int f = 0;
    f |= run_copy(5, "hello", BCOPY_TRUNCATED, "hell");
    f |= run_copy(4, "hello world", BCOPY_TRUNCATED, "hel");
    f |= run_copy(1, "x", BCOPY_TRUNCATED, "");
    return f;
}

static int test_empty_src(void)
{
    return run_copy(3, "", BCOPY_OK, "");
}

static int test_errors(void)
{
    char dst[4] = {'a', 'b', 'c', 'd'};
    size_t copied = 7;
    int f = 0;
    f |= check(bounded_copy(NULL, 4, "x", 1, &copied) == BCOPY_ERR_NULL, "NULL dst");
    f |= check(bounded_copy(dst, 4, NULL, 1, &copied) == BCOPY_ERR_NULL, "NULL src");
    f |= check(bounded_copy(dst, 0, "x", 1, &copied) == BCOPY_ERR_SIZE, "dst_size 0");
    f |= check(copied == 7 && dst[0] == 'a', "nothing written on error");
    f |= check(bounded_copy(dst, 4, "xy", 2, NULL) == BCOPY_OK && strcmp(dst, "xy") == 0, "copied may be NULL");
    f |= check(dst[3] == 'd', "bytes after the terminator untouched");
    return f;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <case>\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "normal") == 0) return test_normal();
    if (strcmp(argv[1], "exact_fit") == 0) return test_exact_fit();
    if (strcmp(argv[1], "truncate") == 0) return test_truncate();
    if (strcmp(argv[1], "empty_src") == 0) return test_empty_src();
    if (strcmp(argv[1], "errors") == 0) return test_errors();
    fprintf(stderr, "unknown case %s\n", argv[1]);
    return 2;
}
