/* Trusted runtime tests for sum_array (run under ASan/UBSan/LSan). */
#include <limits.h>
#include <stdio.h>
#include <string.h>

#include "sum_array.h"

static int check(int cond, const char *what)
{
    if (!cond) {
        fprintf(stderr, "FAILED: %s\n", what);
        return 1;
    }
    return 0;
}

static int test_normal(void)
{
    int a[5] = {1, 2, 3, 4, 5};
    int f = 0;
    f |= check(sum_array(a, 5) == 15, "1+2+3+4+5");
    f |= check(sum_array(a, 1) == 1, "prefix of length 1");
    return f;
}

static int test_long(void)
{
    /* 16 elements: more iterations than the (too small) unwind bound 5. */
    int a[16];
    long long expect = 0;
    for (int k = 0; k < 16; k++) {
        a[k] = (k % 2 == 0) ? INT_MAX : INT_MIN;
        expect += a[k];
    }
    int f = 0;
    f |= check(sum_array(a, 16) == expect, "16 extreme values (no int overflow)");
    int big[3] = {INT_MAX, INT_MAX, INT_MAX};
    f |= check(sum_array(big, 3) == 3LL * INT_MAX, "sum exceeds INT_MAX");
    return f;
}

static int test_empty(void)
{
    int a[1] = {42};
    return check(sum_array(a, 0) == 0, "n == 0");
}

static int test_null(void)
{
    return check(sum_array(NULL, 7) == 0, "NULL array");
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <case>\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "normal") == 0) return test_normal();
    if (strcmp(argv[1], "long") == 0) return test_long();
    if (strcmp(argv[1], "empty") == 0) return test_empty();
    if (strcmp(argv[1], "null") == 0) return test_null();
    fprintf(stderr, "unknown case %s\n", argv[1]);
    return 2;
}
