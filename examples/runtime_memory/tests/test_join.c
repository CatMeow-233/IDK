/* Trusted runtime tests for join_with_sep (run under ASan/UBSan/LSan).
 * Every successful result is freed by the test, so any leak LeakSanitizer
 * reports was caused by join_with_sep itself. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "join.h"

static int check(int cond, const char *what)
{
    if (!cond) {
        fprintf(stderr, "FAILED: %s\n", what);
        return 1;
    }
    return 0;
}

static int expect_ok(const char *a, const char *b, char sep, const char *want)
{
    struct joined r;
    int f = 0;
    int rc = join_with_sep(&r, a, strlen(a), b, strlen(b), sep);
    f |= check(rc == JOIN_OK, "rc == JOIN_OK");
    if (rc == JOIN_OK) {
        f |= check(r.data != NULL && strcmp(r.data, want) == 0, want);
        f |= check(r.len == strlen(want), "len");
        free(r.data);
    }
    return f;
}

static int expect_err(const char *a, size_t a_len, const char *b, size_t b_len, char sep, int want, const char *what)
{
    struct joined r;
    r.data = (char *)"garbage";
    r.len = 99;
    int rc = join_with_sep(&r, a, a_len, b, b_len, sep);
    int f = check(rc == want, what);
    f |= check(r.data == NULL && r.len == 0, "result cleared on error");
    if (rc == JOIN_OK) {
        free(r.data);
    }
    return f;
}

static int test_normal(void)
{
    int f = 0;
    f |= expect_ok("key", "value", '=', "key=value");
    f |= expect_ok("usr", "local", '/', "usr/local");
    return f;
}

static int test_empty_fields(void)
{
    int f = 0;
    f |= expect_ok("", "", ',', ",");
    f |= expect_ok("a", "", ',', "a,");
    f |= expect_ok("", "b", ',', ",b");
    return f;
}

static int test_null_args(void)
{
    int f = 0;
    f |= check(join_with_sep(NULL, "a", 1, "b", 1, ',') == JOIN_ERR_NULL, "NULL res");
    f |= expect_err(NULL, 0, "b", 1, ',', JOIN_ERR_NULL, "NULL a");
    f |= expect_err("a", 1, NULL, 0, ',', JOIN_ERR_NULL, "NULL b");
    f |= expect_err("a", 1, "b", 1, '\0', JOIN_ERR_NULL, "NUL separator");
    return f;
}

static int test_too_long(void)
{
    static char big[JOIN_MAX_FIELD + 1];
    memset(big, 'x', sizeof big);
    int f = 0;
    f |= expect_err(big, sizeof big, "b", 1, ',', JOIN_ERR_TOO_LONG, "first field too long");
    f |= expect_err("a", 1, big, sizeof big, ',', JOIN_ERR_TOO_LONG, "second field too long");
    return f;
}

static int test_separator_in_field(void)
{
    int f = 0;
    f |= expect_err("a,b", 3, "c", 1, ',', JOIN_ERR_FIELD, "separator in first field");
    f |= expect_err("a", 1, "b,c", 3, ',', JOIN_ERR_FIELD, "separator in second field");
    return f;
}

static int test_nul_in_field(void)
{
    int f = 0;
    f |= expect_err("a\0b", 3, "c", 1, ',', JOIN_ERR_FIELD, "NUL in first field");
    f |= expect_err("a", 1, "b\0c", 3, ',', JOIN_ERR_FIELD, "NUL in second field");
    return f;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <case>\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "normal") == 0) return test_normal();
    if (strcmp(argv[1], "empty_fields") == 0) return test_empty_fields();
    if (strcmp(argv[1], "null_args") == 0) return test_null_args();
    if (strcmp(argv[1], "too_long") == 0) return test_too_long();
    if (strcmp(argv[1], "separator_in_field") == 0) return test_separator_in_field();
    if (strcmp(argv[1], "nul_in_field") == 0) return test_nul_in_field();
    fprintf(stderr, "unknown case %s\n", argv[1]);
    return 2;
}
