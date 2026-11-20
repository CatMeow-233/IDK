/* Trusted runtime tests for mul_u64_checked (run under ASan/UBSan/LSan). */
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "mul_checked.h"

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
    uint64_t r = 0;
    int f = 0;
    f |= check(mul_u64_checked(6, 7, &r) == MULCHK_OK && r == 42, "6 * 7");
    f |= check(mul_u64_checked(0, UINT64_MAX, &r) == MULCHK_OK && r == 0, "0 * max");
    f |= check(mul_u64_checked(UINT64_MAX, 1, &r) == MULCHK_OK && r == UINT64_MAX, "max * 1");
    return f;
}

static int test_boundary(void)
{
    uint64_t r = 0;
    int f = 0;
    /* (2^32 - 1) * (2^32 + 1) = 2^64 - 1 fits exactly. */
    f |= check(mul_u64_checked(0xFFFFFFFFu, 0x100000001u, &r) == MULCHK_OK && r == UINT64_MAX, "largest exact product");
    f |= check(mul_u64_checked(UINT64_MAX / 3, 3, &r) == MULCHK_OK && r == UINT64_MAX, "max/3 * 3");
    return f;
}

static int test_overflow(void)
{
    uint64_t r = 99;
    int f = 0;
    f |= check(mul_u64_checked((uint64_t)1 << 32, (uint64_t)1 << 32, &r) == MULCHK_ERR_OVERFLOW, "2^32 * 2^32");
    f |= check(mul_u64_checked(UINT64_MAX, 2, &r) == MULCHK_ERR_OVERFLOW, "max * 2");
    f |= check(mul_u64_checked(UINT64_MAX / 3 + 1, 3, &r) == MULCHK_ERR_OVERFLOW, "(max/3 + 1) * 3");
    f |= check(r == 99, "*out untouched on overflow");
    return f;
}

static int test_null(void)
{
    return check(mul_u64_checked(2, 3, NULL) == MULCHK_ERR_NULL, "NULL out");
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <case>\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "normal") == 0) return test_normal();
    if (strcmp(argv[1], "boundary") == 0) return test_boundary();
    if (strcmp(argv[1], "overflow") == 0) return test_overflow();
    if (strcmp(argv[1], "null") == 0) return test_null();
    fprintf(stderr, "unknown case %s\n", argv[1]);
    return 2;
}
