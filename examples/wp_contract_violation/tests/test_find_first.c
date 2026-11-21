/* Trusted runtime tests for find_first (run under ASan/UBSan). */
#include <stdio.h>
#include <string.h>

#include "find_first.h"

static int check(int cond, const char *what)
{
    if (!cond) {
        fprintf(stderr, "FAILED: %s\n", what);
        return 1;
    }
    return 0;
}

static int test_found(void)
{
    int a[5] = {4, 7, 7, 1, 9};
    size_t pos = 99;
    int f = 0;
    f |= check(find_first(a, 5, 4, &pos) == 1 && pos == 0, "key at index 0");
    f |= check(find_first(a, 5, 7, &pos) == 1 && pos == 1, "first of two occurrences");
    f |= check(find_first(a, 5, 1, &pos) == 1 && pos == 3, "key in the middle");
    return f;
}

static int test_absent(void)
{
    int a[3] = {1, 2, 3};
    size_t pos = 99;
    int f = 0;
    f |= check(find_first(a, 3, 5, &pos) == 0 && pos == 99, "absent key, pos untouched");
    f |= check(find_first(a, 0, 1, &pos) == 0 && pos == 99, "empty array");
    return f;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <case>\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "found") == 0) return test_found();
    if (strcmp(argv[1], "absent") == 0) return test_absent();
    fprintf(stderr, "unknown case %s\n", argv[1]);
    return 2;
}
