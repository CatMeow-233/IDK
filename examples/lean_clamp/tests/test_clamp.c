/* Trusted runtime tests for clamp_int (run under ASan/UBSan/LSan). */
#include <limits.h>
#include <stdio.h>
#include <string.h>

#include "clamp.h"

static int check(int cond, const char *what)
{
    if (!cond) {
        fprintf(stderr, "FAILED: %s\n", what);
        return 1;
    }
    return 0;
}

static int test_inside(void)
{
    int out = 0;
    int f = 0;
    f |= check(clamp_int(5, 0, 10, &out) == CLAMP_OK && out == 5, "inside");
    f |= check(clamp_int(0, 0, 10, &out) == CLAMP_OK && out == 0, "lower edge");
    f |= check(clamp_int(10, 0, 10, &out) == CLAMP_OK && out == 10, "upper edge");
    f |= check(clamp_int(3, 3, 3, &out) == CLAMP_OK && out == 3, "single point");
    return f;
}

static int test_outside(void)
{
    int out = 0;
    int f = 0;
    f |= check(clamp_int(-7, 0, 10, &out) == CLAMP_OK && out == 0, "below");
    f |= check(clamp_int(99, 0, 10, &out) == CLAMP_OK && out == 10, "above");
    f |= check(clamp_int(INT_MIN, -5, 5, &out) == CLAMP_OK && out == -5, "INT_MIN");
    f |= check(clamp_int(INT_MAX, -5, 5, &out) == CLAMP_OK && out == 5, "INT_MAX");
    return f;
}

static int test_errors(void)
{
    int out = 42;
    int f = 0;
    f |= check(clamp_int(1, 10, 0, &out) == CLAMP_ERR_RANGE && out == 42, "empty interval");
    f |= check(clamp_int(1, INT_MAX, INT_MIN, &out) == CLAMP_ERR_RANGE && out == 42, "extreme empty interval");
    f |= check(clamp_int(1, 0, 10, NULL) == CLAMP_ERR_NULL, "NULL out");
    f |= check(clamp_int(1, 10, 0, NULL) == CLAMP_ERR_NULL, "NULL out wins over empty interval");
    return f;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <case>\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "inside") == 0) return test_inside();
    if (strcmp(argv[1], "outside") == 0) return test_outside();
    if (strcmp(argv[1], "errors") == 0) return test_errors();
    fprintf(stderr, "unknown case %s\n", argv[1]);
    return 2;
}
