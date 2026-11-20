/* Trusted runtime tests for checked_add (run under ASan/UBSan/LSan). */
#include <limits.h>
#include <stdio.h>
#include <string.h>

#include "checked_add.h"

static int check(int cond, const char *what)
{
    if (!cond) {
        fprintf(stderr, "FAILED: %s\n", what);
        return 1;
    }
    return 0;
}

static int expect_ok(int a, int b, int want, const char *what)
{
    int out = 12345;
    int rc = checked_add(a, b, &out);
    return check(rc == CHECKED_ADD_OK && out == want, what);
}

static int expect_overflow(int a, int b, const char *what)
{
    int out = 77;
    int rc = checked_add(a, b, &out);
    return check(rc == CHECKED_ADD_ERR_OVERFLOW && out == 77, what);
}

static int test_normal(void)
{
    int f = 0;
    f |= expect_ok(2, 3, 5, "2 + 3");
    f |= expect_ok(-2, 3, 1, "-2 + 3");
    f |= expect_ok(-2, -3, -5, "-2 + -3");
    f |= expect_ok(0, 0, 0, "0 + 0");
    return f;
}

static int test_boundaries(void)
{
    int f = 0;
    f |= expect_ok(INT_MAX, 0, INT_MAX, "INT_MAX + 0");
    f |= expect_ok(INT_MAX - 1, 1, INT_MAX, "(INT_MAX - 1) + 1");
    f |= expect_ok(INT_MIN, 0, INT_MIN, "INT_MIN + 0");
    f |= expect_ok(INT_MIN + 1, -1, INT_MIN, "(INT_MIN + 1) + -1");
    f |= expect_ok(INT_MAX, INT_MIN, -1, "INT_MAX + INT_MIN");
    return f;
}

static int test_positive_overflow(void)
{
    int f = 0;
    f |= expect_overflow(INT_MAX, 1, "INT_MAX + 1");
    f |= expect_overflow(INT_MAX, INT_MAX, "INT_MAX + INT_MAX");
    return f;
}

static int test_negative_overflow(void)
{
    int f = 0;
    f |= expect_overflow(INT_MIN, -1, "INT_MIN + -1");
    f |= expect_overflow(INT_MIN, INT_MIN, "INT_MIN + INT_MIN");
    return f;
}

static int test_null_out(void)
{
    int f = 0;
    f |= check(checked_add(1, 2, NULL) == CHECKED_ADD_ERR_NULL, "NULL out");
    f |= check(checked_add(INT_MAX, 1, NULL) == CHECKED_ADD_ERR_NULL, "NULL out is reported before overflow");
    return f;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <case>\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "normal") == 0) return test_normal();
    if (strcmp(argv[1], "boundaries") == 0) return test_boundaries();
    if (strcmp(argv[1], "positive_overflow") == 0) return test_positive_overflow();
    if (strcmp(argv[1], "negative_overflow") == 0) return test_negative_overflow();
    if (strcmp(argv[1], "null_out") == 0) return test_null_out();
    fprintf(stderr, "unknown case %s\n", argv[1]);
    return 2;
}
