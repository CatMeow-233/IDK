/* Trusted runtime tests for midpoint (run under ASan/UBSan/LSan). */
#include <limits.h>
#include <stdio.h>
#include <string.h>

#include "midpoint.h"

static int check(int cond, const char *what)
{
    if (!cond) {
        fprintf(stderr, "FAILED: %s\n", what);
        return 1;
    }
    return 0;
}

static int test_small(void)
{
    int f = 0;
    f |= check(midpoint(2, 4) == 3, "midpoint(2, 4)");
    f |= check(midpoint(-10, 10) == 0, "midpoint(-10, 10)");
    f |= check(midpoint(7, 7) == 7, "midpoint(7, 7)");
    return f;
}

static int test_odd(void)
{
    int f = 0;
    f |= check(midpoint(1, 1) == 1, "midpoint(1, 1)");
    f |= check(midpoint(1, 2) == 1, "midpoint(1, 2) rounds toward zero");
    f |= check(midpoint(-1, -2) == -1, "midpoint(-1, -2) rounds toward zero");
    f |= check(midpoint(3, 5) == 4, "midpoint(3, 5)");
    return f;
}

static int test_extremes(void)
{
    int f = 0;
    f |= check(midpoint(INT_MAX, INT_MAX) == INT_MAX, "midpoint(INT_MAX, INT_MAX)");
    f |= check(midpoint(INT_MIN, INT_MIN) == INT_MIN, "midpoint(INT_MIN, INT_MIN)");
    f |= check(midpoint(INT_MAX, INT_MIN) == 0, "midpoint(INT_MAX, INT_MIN)");
    f |= check(midpoint(INT_MAX, INT_MAX - 2) == INT_MAX - 1, "midpoint(INT_MAX, INT_MAX - 2)");
    return f;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <case>\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "small") == 0) return test_small();
    if (strcmp(argv[1], "odd") == 0) return test_odd();
    if (strcmp(argv[1], "extremes") == 0) return test_extremes();
    fprintf(stderr, "unknown case %s\n", argv[1]);
    return 2;
}
