/* Trusted runtime tests for array_lookup (run under ASan/UBSan/LSan). */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "array_lookup.h"

static int check(int cond, const char *what)
{
    if (!cond) {
        fprintf(stderr, "FAILED: %s\n", what);
        return 1;
    }
    return 0;
}

/* Heap copy of exactly n ints so that ASan sees the real object bounds. */
static int *make_array(const int *init, size_t n)
{
    int *a = malloc((n ? n : 1) * sizeof *a);
    if (a == NULL) {
        fprintf(stderr, "out of memory\n");
        exit(3);
    }
    if (n) {
        memcpy(a, init, n * sizeof *a);
    }
    return a;
}

static int test_normal(void)
{
    static const int init[5] = {10, 20, 30, 40, 50};
    int *a = make_array(init, 5);
    int out = -1;
    int f = 0;
    f |= check(array_lookup(a, 5, 0, &out) == ARRAY_LOOKUP_OK && out == 10, "first element");
    f |= check(array_lookup(a, 5, 2, &out) == ARRAY_LOOKUP_OK && out == 30, "middle element");
    f |= check(array_lookup(a, 5, 4, &out) == ARRAY_LOOKUP_OK && out == 50, "last element (index == count - 1)");
    f |= check(memcmp(a, init, sizeof init) == 0, "array unchanged");
    free(a);
    return f;
}

/* The off-by-one boundary: index == count must be rejected without reading. */
static int test_index_equals_count(void)
{
    static const int init[3] = {1, 2, 3};
    int *a = make_array(init, 3);
    int out = 77;
    int f = 0;
    f |= check(array_lookup(a, 3, 3, &out) == ARRAY_LOOKUP_ERR_RANGE, "index == count");
    f |= check(out == 77, "out untouched");
    free(a);
    return f;
}

static int test_out_of_range(void)
{
    static const int init[3] = {1, 2, 3};
    int *a = make_array(init, 3);
    int out = 77;
    int f = 0;
    f |= check(array_lookup(a, 3, 4, &out) == ARRAY_LOOKUP_ERR_RANGE, "index == count + 1");
    f |= check(array_lookup(a, 3, SIZE_MAX, &out) == ARRAY_LOOKUP_ERR_RANGE, "index == SIZE_MAX");
    f |= check(out == 77, "out untouched");
    free(a);
    return f;
}

static int test_null(void)
{
    static const int init[2] = {1, 2};
    int *a = make_array(init, 2);
    int out = 5;
    int f = 0;
    f |= check(array_lookup(NULL, 2, 0, &out) == ARRAY_LOOKUP_ERR_NULL, "NULL values");
    f |= check(array_lookup(a, 2, 0, NULL) == ARRAY_LOOKUP_ERR_NULL, "NULL out");
    f |= check(out == 5, "out untouched");
    free(a);
    return f;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <case>\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "normal") == 0) return test_normal();
    if (strcmp(argv[1], "index_equals_count") == 0) return test_index_equals_count();
    if (strcmp(argv[1], "out_of_range") == 0) return test_out_of_range();
    if (strcmp(argv[1], "null") == 0) return test_null();
    fprintf(stderr, "unknown case %s\n", argv[1]);
    return 2;
}
