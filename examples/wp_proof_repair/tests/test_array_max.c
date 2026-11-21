/* Trusted runtime tests for index_of_max (run under ASan/UBSan). */
#include <limits.h>
#include <stdio.h>
#include <string.h>

#include "array_max.h"

static int expect(size_t got, size_t want, const char *what)
{
    if (got != want) {
        fprintf(stderr, "FAILED: %s: got %zu, want %zu\n", what, got, want);
        return 1;
    }
    return 0;
}

static int test_basic(void)
{
    int one[1] = {42};
    int up[5] = {1, 2, 3, 4, 5};
    int down[4] = {9, 7, 5, 3};
    int f = 0;
    f |= expect(index_of_max(one, 1), 0, "single element");
    f |= expect(index_of_max(up, 5), 4, "ascending");
    f |= expect(index_of_max(down, 4), 0, "descending");
    return f;
}

static int test_ties(void)
{
    int ties[6] = {3, 8, 1, 8, 8, 2};
    int same[3] = {5, 5, 5};
    int f = 0;
    f |= expect(index_of_max(ties, 6), 1, "first of several maxima");
    f |= expect(index_of_max(same, 3), 0, "all equal");
    return f;
}

static int test_extremes(void)
{
    int neg[4] = {INT_MIN, -7, INT_MIN, -9};
    int big[3] = {INT_MIN, INT_MAX, 0};
    int f = 0;
    f |= expect(index_of_max(neg, 4), 1, "negative values");
    f |= expect(index_of_max(big, 3), 1, "INT_MAX / INT_MIN");
    return f;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <case>\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "basic") == 0) return test_basic();
    if (strcmp(argv[1], "ties") == 0) return test_ties();
    if (strcmp(argv[1], "extremes") == 0) return test_extremes();
    fprintf(stderr, "unknown case %s\n", argv[1]);
    return 2;
}
