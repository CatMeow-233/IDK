/* Trusted runtime tests for safe_array_get (run under ASan/UBSan). */
#include <stdio.h>
#include <string.h>

#include "safe_array.h"

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
    int a[5] = {10, 20, 30, 40, 50};
    int out = -1;
    int f = 0;
    f |= check(safe_array_get(a, 5, 0, &out) == SAFE_ARRAY_OK && out == 10, "first element");
    f |= check(safe_array_get(a, 5, 4, &out) == SAFE_ARRAY_OK && out == 50, "last element");
    return f;
}

static int test_out_of_range(void)
{
    int a[3] = {1, 2, 3};
    int out = 77;
    int f = 0;
    f |= check(safe_array_get(a, 3, 3, &out) == SAFE_ARRAY_ERR_RANGE, "idx == len");
    f |= check(safe_array_get(a, 3, (size_t)-1, &out) == SAFE_ARRAY_ERR_RANGE, "idx == SIZE_MAX");
    f |= check(safe_array_get(a, 0, 0, &out) == SAFE_ARRAY_ERR_RANGE, "empty array");
    f |= check(out == 77, "out untouched");
    return f;
}

static int test_null(void)
{
    int a[2] = {1, 2};
    int out = 0;
    int f = 0;
    f |= check(safe_array_get(NULL, 2, 0, &out) == SAFE_ARRAY_ERR_NULL, "NULL arr");
    f |= check(safe_array_get(a, 2, 0, NULL) == SAFE_ARRAY_ERR_NULL, "NULL out");
    return f;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <case>\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "normal") == 0) return test_normal();
    if (strcmp(argv[1], "out_of_range") == 0) return test_out_of_range();
    if (strcmp(argv[1], "null") == 0) return test_null();
    fprintf(stderr, "unknown case %s\n", argv[1]);
    return 2;
}
