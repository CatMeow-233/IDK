/* Trusted runtime tests for safe_ratio (run under ASan/UBSan/LSan). */
#include <limits.h>
#include <stdio.h>
#include <string.h>

#include "safe_ratio.h"

static int check(int cond, const char *what)
{
    if (!cond) {
        fprintf(stderr, "FAILED: %s\n", what);
        return 1;
    }
    return 0;
}

static int expect_ok(int num, int den, int want, const char *what)
{
    int out = 12345;
    int rc = safe_ratio(num, den, &out);
    return check(rc == SAFE_RATIO_OK && out == want, what);
}

static int test_normal(void)
{
    int f = 0;
    f |= expect_ok(7, 2, 3, "7 / 2");
    f |= expect_ok(-7, 2, -3, "-7 / 2 truncates toward zero");
    f |= expect_ok(7, -2, -3, "7 / -2");
    f |= expect_ok(-7, -2, 3, "-7 / -2");
    f |= expect_ok(0, 5, 0, "0 / 5");
    f |= expect_ok(6, 3, 2, "exact division");
    return f;
}

static int test_extremes(void)
{
    int f = 0;
    f |= expect_ok(INT_MAX, 1, INT_MAX, "INT_MAX / 1");
    f |= expect_ok(INT_MAX, -1, -INT_MAX, "INT_MAX / -1");
    f |= expect_ok(INT_MIN, 1, INT_MIN, "INT_MIN / 1");
    f |= expect_ok(INT_MIN, 2, INT_MIN / 2, "INT_MIN / 2");
    f |= expect_ok(INT_MIN, INT_MIN, 1, "INT_MIN / INT_MIN");
    f |= expect_ok(-1, INT_MIN, 0, "-1 / INT_MIN");
    return f;
}

static int test_zero_den(void)
{
    int out = 77;
    int f = 0;
    f |= check(safe_ratio(5, 0, &out) == SAFE_RATIO_ERR_DIV_ZERO, "5 / 0");
    f |= check(safe_ratio(0, 0, &out) == SAFE_RATIO_ERR_DIV_ZERO, "0 / 0");
    f |= check(out == 77, "out untouched");
    return f;
}

static int test_int_min_by_minus_one(void)
{
    int out = 77;
    int f = 0;
    f |= check(safe_ratio(INT_MIN, -1, &out) == SAFE_RATIO_ERR_OVERFLOW, "INT_MIN / -1");
    f |= check(out == 77, "out untouched");
    return f;
}

static int test_null_out(void)
{
    int f = 0;
    f |= check(safe_ratio(1, 1, NULL) == SAFE_RATIO_ERR_NULL, "NULL out");
    f |= check(safe_ratio(1, 0, NULL) == SAFE_RATIO_ERR_NULL, "NULL out is reported before den == 0");
    return f;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <case>\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "normal") == 0) return test_normal();
    if (strcmp(argv[1], "extremes") == 0) return test_extremes();
    if (strcmp(argv[1], "zero_den") == 0) return test_zero_den();
    if (strcmp(argv[1], "int_min_by_minus_one") == 0) return test_int_min_by_minus_one();
    if (strcmp(argv[1], "null_out") == 0) return test_null_out();
    fprintf(stderr, "unknown case %s\n", argv[1]);
    return 2;
}
