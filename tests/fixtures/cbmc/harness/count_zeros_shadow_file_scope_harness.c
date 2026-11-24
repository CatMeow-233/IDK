/* The aliased probe calls harness code (a shadowed __CPROVER_assert) in the vacuity build only. */
#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "cbmc_fx.h"

#define MAX_N 8
#define ARM LEANC_REACH

static int armed;

static void set_armed(int c, const char *m)
{
    (void)c;
    (void)m;
    armed = 1;
}

void __CPROVER_assert(int c, const char *m)
{
    set_armed(c, m);
}

static void arm(void)
{
    ARM("arm");
}

int main(void)
{
    size_t n = nondet_size_t();
    LEANC_ASSUME(n <= MAX_N);
    int *a = malloc(n * sizeof(int));
    arm();
    if (armed) {
        size_t z = count_zeros(a, n);
        LEANC_REACH("after_call");
        assert(z > n);
    }
    free(a);
    return 0;
}
